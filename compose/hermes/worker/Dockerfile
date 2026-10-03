FROM alpine:3.21

# bash: Hermes runs remote commands with bash -l.
# tar: Hermes snapshots with GNU tar (--no-overwrite-dir); BusyBox tar cannot.
# python3: execute_code runs on this backend.
# git + ca-certificates: workspace git, including HTTPS.
# ripgrep: search_files prefers rg.
# No Node or browser here. Bootstrap sets bot_desktop.placement=gateway
# so those stay in the hermes container.
RUN apk add --no-cache \
        openssh-server \
        bash \
        tar \
        python3 \
        git \
        ca-certificates \
        ripgrep \
    && ssh-keygen -A \
    && mkdir -p /etc/hermes-worker /run /var/empty

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
