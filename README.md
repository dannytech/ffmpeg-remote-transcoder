# ffmpeg-remote-transcoder

ffmpeg-based remote transcoding script (inspired by rffmpeg). Documentation will refer to ffmpeg-remote-transcoder as FRT for simplicity.

FRT is designed for many-to-one remote transcoding, allowing multiple clients to use a single remote transcoding server. FRT works by sharing files from the client to the server using symlinks in a Samba share, then calling Ffmpeg via SSH on the mounted share. This way, you can transcode files from anywhere on the client without having to wait for the file to copy to the server.

## Installation

Before installing FRT, the client and server must be connected using a file share so that the server has near direct access to limited client files. Because FRT uses symlinks to connect files to the working directory, a file server supporting symlink resolution outside of the share must be used. Samba server is used in the reference implementation for this reason. A simple share configuration like below will suffice:

```ini
[transcode]
   comment = Videos for remote transcoding
   path = /opt/frt
   writable = yes
   follow symlinks = yes
   wide links = yes
```

Also create a Samba client user and set their password with `smbpasswd -a <user>`. You must create the working directory to be shared (usually `/opt/frt/`), and allow both the Samba client user and the user which will be running FRT full access to it. Then, simply mount the share on the server in such a way that it is readable by the user configured later.

There are two ways to install the FRT script. The first method is to simply place `frt.py` anywhere on disk, then point an application like Jellyfin to this script path. In Jellyfin, this means going to `Playback > Transcoding` in the Dashboard, and updating the `FFmpeg path` to the absolute path of `frt.py`. The alternative is to symlink the `ffmpeg` and `ffprobe` binaries in `/usr/bin/` to `frt.py`, though this requires moving `ffmpeg` and `ffprobe` elsewhere and changing the `Client/FfmpegPath` and `Client/FfprobePath` settings to the new binary location.

## Configuration

Copy the sample configuration file to `/etc/frt.conf`. The settings are documented below, by section:

* `Server`
    * `Host`: The hostname or IP used to connect to the transcoding server
    * `Username`: The SSH username, should be created on the server and have access to the transcode location
    * `IdentityFile` (optional): An SSH private key to use for authentication with the server
    * `WorkingDirectory`: The location of the mounted working directory on the server
    * `FfmpegPath` (optional, default `/usr/bin/ffmpeg`): The location of the Ffmpeg binary on the server
    * `FfprobePath` (optional, default `/usr/bin/ffprobe`): The location of the Ffprobe binary on the server
* `Client`
    * `WorkingDirectory` (optional, default `/opt/frt`): A working directory for FRT to create symlinks in, shared by Samba
    * `FfmpegPath` (optional, default `/usr/bin/ffmpeg`): The location of the fallback Ffmpeg binary on the client
    * `FfprobePath` (optional, default `/usr/bin/ffprobe`): The location of the fallback Ffprobe binary on the client
* `Logging`
    * `LogFile` (optional, default `/var/log/frt.log`): The log destination file, must be writable by the user running the FRT script