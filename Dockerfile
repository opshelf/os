FROM ghcr.io/astral-sh/uv:debian-slim as base

ENV PATH=/root/.local/bin:$PATH
ENV RUN_SEED_SCRIPT=true
ENV UV_PYTHON=3.13

RUN apt-get update && apt-get -y install --no-install-recommends \
	libgl1 \
	libglib2.0-0 \
	libusb-1.0-0 \
	build-essential \
	gcc \
	swig \
	git \
	ca-certificates \
	curl \
	wget \
	v4l-utils \
	cups-bsd \
	cups-daemon \
	network-manager \
	&& rm -rf /var/lib/apt/lists/* \
	&& apt-get clean

RUN git clone --depth 1 https://github.com/joan2937/lg /tmp/lg && \
	cd /tmp/lg && make && make install && \
	rm -rf /tmp/lg

	
WORKDIR /app

COPY openshelf_hardware/ /app/openshelf_hardware/

RUN mkdir -p /app/openshelf_hardware/data && \
	chmod -R 777 /app/openshelf_hardware/data && \
	echo 'export "PATH=$PATH:my_path"' >> /etc/profile

WORKDIR /app/openshelf_hardware
RUN uv sync && uv pip install -e packages/hardware -e packages/openshelf_os -e packages/openshelf_os_server

WORKDIR /app/openshelf_hardware

# RUN uv pip install debugpy

EXPOSE 8000
RUN chmod +x /app/openshelf_hardware/packages/openshelf_os_server/entrypoint.sh

ENTRYPOINT ["/bin/sh", "/app/openshelf_hardware/packages/openshelf_os_server/entrypoint.sh"]
