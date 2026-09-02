#!/bin/sh
# Run the app as an unprivileged user.
#
# The image starts as root only long enough to make the data directory
# writable by that user — a volume created by an earlier version, or a bind
# mount, is typically owned by root — and then drops privileges for good.
# If the container is already started as a non-root user (compose `user:`),
# there is nothing to fix and the command runs as-is.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R restruo:restruo /data
    exec setpriv --reuid=restruo --regid=restruo --init-groups "$@"
fi

exec "$@"
