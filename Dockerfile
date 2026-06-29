FROM rust:1.89-bookworm AS builder

WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY examples ./examples
COPY web ./web

RUN cargo build --release -p vod-strm-builder --bin vod-strm-builder-rs

FROM debian:bookworm-slim

ENV RUST_LOG=info \
    VSB_BIND=0.0.0.0:8080 \
    VSB_WORK_DIR=/work

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /work /media/movies /media/tvshows \
    && chown -R 999:996 /work /media

COPY --from=builder /app/target/release/vod-strm-builder-rs /usr/local/bin/vod-strm-builder-rs

USER 999:996
WORKDIR /work
EXPOSE 8080

CMD ["/usr/local/bin/vod-strm-builder-rs", "serve"]
