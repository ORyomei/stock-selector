FROM ubuntu:24.04

RUN apt-get update && apt-get install -y git curl wget ca-certificates sudo \
    && rm -rf /var/lib/apt/lists/* \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu
