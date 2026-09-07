# The pipeline worker.
#
# One image, replacing three. Two of them were AWS Lambda images
# (`public.ecr.aws/lambda/python`) that differed only in whether ffmpeg was
# present -- the split existed because sizing every activity for the one that
# handles the actual video would have been wasteful when you pay per
# invocation-millisecond. A long-running process pays once, so they collapse.
#
# The third was a Playwright image for the trend scout. That was for TikTok-Api,
# which signed every request inside a live browser page and is retired; the
# three live sources -- Apify, the YouTube Data API and Google Trends -- are
# plain HTTP, so no browser is needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg and ffprobe, for the quality check and for assembling the fal
# end-to-end lane. Debian's package is fine here; the static build the Lambda
# image downloaded was only ever a workaround for Amazon Linux having no ffmpeg
# package at all.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md* ./
COPY pipeline ./pipeline
RUN python -m pip install --no-cache-dir .

# Advances productions, runs the reconcilers, and dispatches trend runs -- the
# state machine, the two Lambdas and the eight schedules, in one process.
CMD ["python", "-m", "pipeline.driver.worker"]
