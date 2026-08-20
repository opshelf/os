FROM ghcr.io/astral-sh/uv:debian-slim as builder

ENV PATH=/root/.local/bin:$PATH
ENV UV_PYTHON=3.13
ENV UV_CACHE_DIR=/tmp/uv-cache

RUN apt-get update && apt-get -y install --no-install-recommends \
	build-essential \
	swig \
	git \
	ca-certificates \
	&& rm -rf /var/lib/apt/lists/* \
	&& apt-get clean

# the pip lgpio wheel links against the shared lib from this build
RUN git clone --depth 1 https://github.com/joan2937/lg /tmp/lg && \
	cd /tmp/lg && git fetch --depth 1 origin bcccd782eceedc5b278b3056ea81d5fbbb89c489 && \
	git checkout FETCH_HEAD && make && make install && \
	rm -rf /tmp/lg

WORKDIR /app
COPY openshelf_hardware/ /app/openshelf_hardware/
WORKDIR /app/openshelf_hardware

RUN uv sync --no-group dev \
	&& uv pip install -e packages/hardware -e packages/openshelf_os -e packages/openshelf_os_server \
	&& rm -rf "$UV_CACHE_DIR"

FROM ghcr.io/astral-sh/uv:debian-slim as runtime

ENV PATH=/root/.local/bin:$PATH
ENV UV_PYTHON=3.13

RUN apt-get update && apt-get -y install --no-install-recommends \
	libglib2.0-0 \
	libusb-1.0-0 \
	network-manager \
	cups-bsd \
	cups-daemon \
	ca-certificates \
	curl \
	&& rm -rf /var/lib/apt/lists/* \
	&& apt-get clean

# the venv runs on the uv-managed CPython; provision the same interpreter
RUN uv python install 3.13

# the compiled lgpio extension links against this shared lib
COPY --from=builder /usr/local/lib/liblgpio.so.1 /usr/local/lib/
RUN ldconfig

WORKDIR /app
COPY --from=builder /app/openshelf_hardware /app/openshelf_hardware

RUN mkdir -p /app/openshelf_hardware/data && \
	chmod -R 777 /app/openshelf_hardware/data

EXPOSE 8000
RUN chmod +x /app/openshelf_hardware/packages/openshelf_os_server/entrypoint.sh

ENTRYPOINT ["/bin/sh", "/app/openshelf_hardware/packages/openshelf_os_server/entrypoint.sh"]
