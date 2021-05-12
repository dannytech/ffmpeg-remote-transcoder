#!/usr/bin/env python3

import logging
import configparser
import subprocess
import signal
import uuid
import sys
import os
import re

# Create a job identifier (used to uniquely identify files)
job = uuid.uuid4().hex

# Create a new logger for this job
log = logging.getLogger(f"ffmpeg-remote-transcoder-{job[:6]}")

# Load the ffmpeg-remote-transcoder configuration
config = configparser.ConfigParser()
config.read("/etc/frt.conf")

# Validate that the required parameters are set
required_params = (("Server", "Host"), ("Server", "Username"), ("Server", "WorkingDirectory"))
for param in required_params:
    if not config.has_option(*param):
        log.error(f"Missing required configuration option {param[0]}/{param[1]}")
        exit()

# Parse the ffmpeg arguments to passthrough
ffmpeg_args = sys.argv[1:]

# Commands that should be redirected to stdout
commands_bypass = { "-help", "-h", "-version", "-encoders", "-decoders" }
bypass = len([ cmd for cmd in commands_bypass if cmd in ffmpeg_args ]) > 0

# Linked files to dereference later
linked = []

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

    # Don't perform server validation
    ssh_command.extend([ "-o", "StrictHostKeyChecking=no" ])
    ssh_command.extend([ "-o", "UserKnownHostsFile=/dev/null" ])

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

def convert_references(ffmpeg_command, dir):
    """
    Convert file references to temporary job symlinks

    :param ffmpeg_command: The ffmpeg command to convert
    :param dir: The working directory to reference links towards
    """
    # Convert the infile reference to point to the job reference
    if "-i" in ffmpeg_command:
        infile = ffmpeg_command.index("-i")

        if not ffmpeg_command[infile].startswith("pipe:"):
            # Link up the source file
            source = link(ffmpeg_command[infile + 1], "src")

            # Replace the infile reference
            ffmpeg_command[infile + 1] = os.path.join(dir, source)
        else:
            None

    # Convert the outfile reference to point to the job reference
    if not bypass and not ffmpeg_command[-1].startswith("pipe:"):
        # Link up the destination file
        dest = link(ffmpeg_command[-1], "dest")

        # Replace the outfile reference
        ffmpeg_command[-1] = os.path.join(dir, dest)
    else:
        None

def generate_ffmpeg_command(context="Server"):
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

    for arg in ffmpeg_args:
        # Escape malformed arguments (such as those including whitespace and invalid characters)
        if re.search(r"/[*()\s|\[\]]/", arg):
            ffmpeg_command.append(f"\"{arg}\"")
        else:
            ffmpeg_command.append(arg)
        
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

def link(file, type):
    """
    Link the source and destination files to the working directory

    :param file: The source file to symlink to the working directory, can be null if unlinking
    :param type: The file purpose, usually "src" or "dest"

    :returns: The filename of the newly created link
    """
    dir = config.get("Client", "WorkingDirectory", fallback="/opt/frt/")

    # Create the filename using the job, source/destination type, and file extension
    ext = os.path.splitext(file)
    name = f"{job}-{type}{ext[1]}"

    # Join the working directory and the new link together
    link = os.path.join(dir, name)

    # Create or reuse the link
    if not os.path.islink(link):
        # Link the file
        os.symlink(file, link)
        linked.append(name)

        log.info(f"Created link {name}")
    else:
        log.info(f"Using existing link {name}")
    
    return name

def run_ffmpeg_remote():
    """
    Run the ffmpeg command on the remote host, handing I/O as necessary

    :returns: The return code from the ffmpeg process
    """
    ssh_command = generate_ssh_command()
    ffmpeg_command = generate_ffmpeg_command()

    # Predict the location of shared files on the server
    dir = config.get("Server", "WorkingDirectory")
    convert_references(ffmpeg_command, dir)

    # Remap the standard in, out, and error to properly handle data streams
    (stdin, stdout, stderr) = map_std(ffmpeg_command)

    log.info("Running ffmpeg command on remote server...")
    log.info(ffmpeg_command)

    # Run the command on the remote host
    proc = subprocess.run(ssh_command + ffmpeg_command, shell=False, bufsize=0, universal_newlines=True, stdin=stdin, stdout=stdout, stderr=stderr)

    # Fall back to local ffmpeg if SSH could not connect
    if proc.returncode == 255:
        log.error("Failed to connect to remote host")
        return run_ffmpeg_local()
    
    # Return the ffmpeg return code
    return proc.returncode

def run_ffmpeg_local():
    """
    Run the ffmpeg command on the local host, handling I/O as necessary

    :returns: The return code from the ffmpeg process
    """
    ffmpeg_command = generate_ffmpeg_command(context="Client")

    # Link files to the working directory
    dir = config.get("Client", "WorkingDirectory", fallback="/opt/frt/")
    convert_references(ffmpeg_command, dir)

    # Remap the standard in, out, and error to properly handle data streams
    (stdin, stdout, stderr) = map_std(ffmpeg_command)

    log.info("Running ffmpeg command local host...")
    log.info(ffmpeg_command)

    # Run the command
    proc = subprocess.run(ffmpeg_command, shell=False, bufsize=0, universal_newlines=True, stdin=stdin, stdout=stdout, stderr=stderr)

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

    # Unlink the source and destination files
    dir = config.get("Client", "WorkingDirectory", fallback="/opt/frt/")
    
    while len(linked) > 0:
        link = linked.pop()

        # Remove the symlink
        os.unlink(os.path.join(dir, link))

        log.info(f"Destroyed link {link}")
    
    log.info("Cleaned up, exiting")

    exit()

def main():
    # Clean up after crashed
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGQUIT, cleanup)
    signal.signal(signal.SIGHUP, cleanup)

    # Configure logging
    logfile = config.get("Logging", "LogFile", fallback="/var/log/frt.log")
    logging.basicConfig(filename=logfile, level=logging.INFO)

    log.info("Beginning remote transcoding...")

    # Run ffmpeg on the remote host
    status = run_ffmpeg_remote()

    if status == 0:
        log.info(f"ffmpeg finished with return code {status}")
    else:
        log.error(f"ffmpeg exited with return code {status}")
    
    cleanup()

if __name__ == "__main__":
    main()
