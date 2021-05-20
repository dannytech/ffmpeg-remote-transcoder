#!/usr/bin/env python3

import logging
import configparser
import subprocess
import signal
import uuid
import sys
import os
import re

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Create a job identifier (used to uniquely identify files)
job = uuid.uuid4().hex

# Create a new logger for this job
log = logging.getLogger(f"ffmpeg-remote-transcoder-{job[:6]}")

# Load the ffmpeg-remote-transcoder configuration
config = configparser.ConfigParser()
config.read("/etc/frt.conf")

# Configure logging
logfile = config.get("Logging", "LogFile", fallback="/var/log/frt.log")
logging.basicConfig(filename=logfile, level=logging.INFO)

# Validate that the required parameters are set
required_params = (("Server", "Host"), ("Server", "Username"), ("Server", "WorkingDirectory"))
for param in required_params:
    if not config.has_option(*param):
        log.error(f"Missing required configuration option {param[0]}/{param[1]}")
        exit()

# Create the local working directory
localdir = os.path.join(config.get("Client", "WorkingDirectory", fallback="/opt/frt/"), job)
os.makedirs(localdir)

# Predict the remote mounted working directory
remotedir = os.path.join(config.get("Server", "WorkingDirectory"), job)

# Parse the ffmpeg arguments to passthrough
ffmpeg_args = sys.argv[1:]

# Commands that should be redirected to stdout
commands_bypass = { "-help", "-h", "-version", "-encoders", "-decoders", "-hwaccels" }
bypass = len([ cmd for cmd in commands_bypass if cmd in ffmpeg_args ]) > 0

# Event handler for working directory changes
class WorkingDirectoryMonitor(FileSystemEventHandler):
    def paths(self, event):
        # Generate the various paths representing a single file
        working = os.path.join(localdir, event.src_path)
        relative = os.path.relpath(working, localdir)
        absolute = os.path.join("/", relative)

        return working, absolute

    def on_created(self, event):
        working, absolute = self.paths(event)

        # Ignore infile references and existing reverse references (both have a file on the other end)
        if not os.path.exists(absolute):
            # Link the destination output to the working copy
            os.link(working, absolute)

            log.info(f"Linked destination file {absolute}")

    def on_deleted(self, event):
        _, absolute = self.paths(event)

        # Check for already linked files
        if os.path.exists(absolute):
            # Remove the file
            os.unlink(absolute)

            log.info(f"Unlinked destination file {absolute}")

def generate_ssh_command():
    """
    Generates an SSH command to connect to the remote host

    :returns: A complete SSH command to prepend another command run on the remote host
    """
    log.info("Generating SSH command...")

    ssh_command = []

    # Add the SSH command itself
    ssh_command.extend(["ssh", "-q" ])

    # Set connection timeouts to fail fast
    ssh_command.extend([ "-o", "ConnectTimeout=1" ])
    ssh_command.extend([ "-o", "ConnectionAttempts=1" ])

    # Don't fall back to interactive authentication
    ssh_command.extend([ "-o", "BatchMode=yes" ])

    # Don't perform server validation
    ssh_command.extend([ "-o", "StrictHostKeyChecking=no" ])
    ssh_command.extend([ "-o", "UserKnownHostsFile=/dev/null" ])

    # Create a persistent session to avoid the latency of setting up a tunnel for each subsequent FRT execution
    persist = config.get("Server", "Persist", fallback=120)
    ssh_command.extend([ "-o", "ControlMaster=auto" ])
    ssh_command.extend([ "-o", "ControlPath=/run/shm/ssh-%r@%h:%p" ])
    ssh_command.extend([ "-o", f"ControlPersist={persist}" ])

    # Load SSH key for authentication
    key = config.get("Server", "IdentityFile", fallback=None)
    if key is not None:
        ssh_command.extend([ "-i", key ])

    # Load the remote host configuration
    username = config.get("Server", "Username")
    host = config.get("Server", "Host")

    # Add login information
    ssh_command.append(f"{username}@{host}")
    
    return ssh_command

def forward_reference(ffmpeg_command):
    """
    Link source files to the working directory

    :param ffmpeg_command: The ffmpeg command to parse
    """
    # Find and replace all file references with links
    for i, arg in enumerate(ffmpeg_command):
        # Detect if this is specifically indicated to be a file
        is_file = arg.startswith("file:")
        if is_file:
            arg = arg[5:]

        # If the argument appears to be a normal file (the extension must contain a letter, to avoid linking timestamps)
        extension = os.path.splitext(arg)[1]
        if is_file or re.search(r"^\.(?=.*[a-zA-Z]).*$", extension):
            absolute = os.path.abspath(arg)

            relative = os.path.relpath(absolute, "/")

            local_working = os.path.join(localdir, relative)
            remote_working = os.path.join(remotedir, relative)

            # Create all directories in the path
            os.makedirs(os.path.dirname(local_working), exist_ok=True)

            # Link source files properly
            if ffmpeg_command[i - 1] == "-i" and not os.path.islink(local_working):
                os.symlink(absolute, local_working)

                log.info(f"Linked source file {absolute}")

            # Replace paths with adjusted remote working paths
            ffmpeg_command[i] = f"file:{remote_working}"

            # Note that no links are made for destination files as these are detected and linked at runtime

def reverse_reference():
    """
    Detects and links output files from ffmpeg to their final destination

    :returns: The file system observer, for easy stopping
    """
    # Create a new monitor
    monitor = WorkingDirectoryMonitor()

    # Create a file system observer
    observer = Observer()
    observer.schedule(monitor, localdir, recursive=True)

    # Begin monitoring the directory for changes
    observer.start()

    return observer

def generate_ffmpeg_command(context):
    """
    Generate a properly escaped and transformed ffmpeg commandline

    :returns: An ffmpeg/ffprobe command which can be run using SSH
    """
    log.info("Generating ffmpeg command...")

    ffmpeg_command = []

    # Start with the command that was used to run this script (should be ffmpeg or ffprobe)
    if "ffprobe" in sys.argv[0]:
        ffmpeg_command.append(config.get(context, "FfprobePath", fallback="/usr/bin/ffprobe"))
    else:
        ffmpeg_command.append(config.get(context, "FfmpegPath", fallback="/usr/bin/ffmpeg"))

    ffmpeg_command.extend(ffmpeg_args)

    # Update file links and prepare working directory
    forward_reference(ffmpeg_command)

    for i, arg in enumerate(ffmpeg_command):
        # Escape malformed arguments (such as those including whitespace and invalid characters)
        if re.search(r"[*()\s|\[\]]", arg):
            ffmpeg_command[i] = f"\"{arg}\""
        
    return ffmpeg_command

def map_std(ffmpeg_command):
    """
    Map standard in, out, and error based on the command that is being run

    :param command: The ffmpeg command line which will be run
    :returns: The standard in, out, and error to utilize when running ffmpeg
    """
    log.info("Remapping standard in/out/error...")

    # Redirect this program's stdout to stderr to prevent it interfering in a data stream
    stdin = sys.stdin
    stdout = sys.stderr
    stderr = sys.stderr

    # Redirect stdout to stdout if a bypassing command or ffprobe is being run
    if bypass or "ffprobe" in ffmpeg_command[0]:
        stdout = sys.stdout
    
    return (stdin, stdout, stderr)

def run_ffmpeg_command(context="Server"):
    """
    Run the ffmpeg command, remapping I/O as necessary

    :param context: Whether to run the command on the server or the client
    :returns: The return code from the ffmpeg process
    """
    ssh_command = generate_ssh_command()
    ffmpeg_command = generate_ffmpeg_command(context)

    # Remap the standard in, out, and error to properly handle data streams
    (stdin, stdout, stderr) = map_std(ffmpeg_command)

    log.info(f"Running ffmpeg command on {context.lower()}...")
    log.info(ffmpeg_command)

    # Determine whether to run the command locally or on the server
    if context == "Server":
        command = ssh_command + ffmpeg_command
    elif context == "Client":
        command = ffmpeg_command

    # Begin watching for new files in the working directory
    observer = reverse_reference()

    # Run the command
    proc = subprocess.run(command, shell=False, bufsize=0, universal_newlines=True, stdin=stdin, stdout=stdout, stderr=stderr)

    # Stop watching for file system changes
    observer.stop()

    # Wait for the monitor thread to terminate
    observer.join()

    # Fall back to local ffmpeg if SSH could not connect
    if context == "Server" and proc.returncode == 255:
        log.error("Failed to connect to remote host")
        return run_ffmpeg_command(context="Client")
    
    # Return the ffmpeg return code
    return proc.returncode

def cleanup(signum="", frame=""):
    """
    Cleans up local and remote files and processes, then exits
    """
    # Assemble variables needed for remote cleanup
    ssh_command = generate_ssh_command()
    user = config.get("Server", "Username")

    # Creates a command to filter and kill orphaned processes owned by the current user
    kill_command = [ "pkill", "-P1", "-u", user, "-f", "\"ffmpeg|ffprobe\"" ]
    
    # Kill all orphaned processes
    log.info(f"Running cleanup command on remote server...")
    log.info(kill_command)
    
    subprocess.run(ssh_command + kill_command)
    
    log.info("Unlinking file references...")
    for root, _, files in os.walk(localdir, topdown=False):
        for file in files:
            working = os.path.join(root, file)

            # Remove the working side of the hard/soft link, the other end will be preserved
            os.unlink(working)

        # Remove the current directory (walking starts from the lowest level)
        os.rmdir(root)
    
    log.info("Cleaned up, exiting")

    exit()

def main():
    # Clean up after crashed
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGQUIT, cleanup)
    signal.signal(signal.SIGHUP, cleanup)

    log.info("Beginning transcoding...")

    # Run ffmpeg on the remote host
    status = run_ffmpeg_command()

    if status == 0:
        log.info(f"ffmpeg finished with return code {status}")
    else:
        log.error(f"ffmpeg exited with return code {status}")
    
    cleanup()

if __name__ == "__main__":
    main()
