# Docker Timezone And Telegram Log Safety Design

**Goal**

Make the Docker deployment use `Asia/Shanghai` by default and prevent Telegram bot tokens from appearing in application logs.

**Scope**

- Set the container timezone to `Asia/Shanghai` in both the image and compose deployment.
- Add a logging filter that redacts Telegram bot tokens from log messages, including `httpx` request logs.
- Add a focused unit test for token redaction behavior.

**Approach**

- Use Docker configuration for timezone instead of application-level clock overrides.
- Redact Telegram secrets at the logging boundary so behavior stays unchanged and only log output is affected.
- Verify with one unit test plus container runtime checks after rebuild.
