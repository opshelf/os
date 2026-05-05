FROM ghcr.io/astral-sh/uv:debian-slim as base

ENV PATH=/root/.local/bin:$PATH
ENV RUN_SEED_SCRIPT=true
ENV UV_PYTHON=3.13

RUN apt-get update && apt-get -y install --no-install-recommends \
	libgl1 \
	libglib2.0-0 \
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
	&& rm -rf /var/lib/apt/lists/* \
	&& apt-get clean

RUN git clone --depth 1 https://github.com/joan2937/lg /tmp/lg && \
	cd /tmp/lg && make && make install && \
	rm -rf /tmp/lg

	
WORKDIR /app

COPY perpetual_hardware/ /app/perpetual_hardware/

RUN mkdir -p /app/perpetual_hardware/data && \
	chmod -R 777 /app/perpetual_hardware/data && \
	echo 'export "PATH=$PATH:my_path"' >> /etc/profile

WORKDIR /app/perpetual_hardware
RUN uv sync && uv pip install -e packages/hardware -e packages/perpetual_os1 -e packages/perpetual_os1_server

WORKDIR /app/perpetual_hardware

EXPOSE 8000
RUN chmod +x /app/perpetual_hardware/packages/perpetual_os1_server/entrypoint.sh

ENTRYPOINT ["/bin/sh", "/app/perpetual_hardware/packages/perpetual_os1_server/entrypoint.sh"]
