FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
