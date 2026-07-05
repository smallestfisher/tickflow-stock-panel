# Docker Timezone And Telegram Log Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Docker deployment show Beijing time and stop Telegram bot tokens from leaking into logs.

**Architecture:** Timezone is fixed at the deployment boundary with Docker image and compose settings. Telegram token redaction is handled by a logging filter applied to process handlers so library logs are sanitized without changing request behavior.

**Tech Stack:** Docker Compose, Python logging, pytest

---

### Task 1: Add Telegram token redaction test and implementation

**Files:**
- Modify: `backend/tests/test_telegram_adapter.py`
- Modify: `backend/app/services/telegram_adapter.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write the failing test**

```python
def test_mask_telegram_token_redacts_bot_url():
    raw = "GET https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456/sendMessage"
    masked = mask_telegram_token(raw)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in masked
    assert "bot123456:" in masked
    assert "***" in masked
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_telegram_adapter.py -q`
Expected: FAIL because `mask_telegram_token` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
_TOKEN_RE = re.compile(r"(https://api\\.telegram\\.org/bot)(\\d+):([^/\\s?]+)")

def mask_telegram_token(text: str) -> str:
    return _TOKEN_RE.sub(r"\\1\\2:***", text or "")

class TelegramTokenMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = mask_telegram_token(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_telegram_adapter.py -q`
Expected: PASS

- [ ] **Step 5: Wire the filter into app startup**

```python
for handler in logging.getLogger().handlers:
    handler.addFilter(TelegramTokenMaskingFilter())
```

### Task 2: Set Docker timezone defaults

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add image-level timezone defaults**

```dockerfile
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ >/etc/timezone
```

- [ ] **Step 2: Add compose-level timezone override**

```yaml
environment:
  TZ: Asia/Shanghai
```

- [ ] **Step 3: Verify compose renders the env**

Run: `docker compose config`
Expected: service `app` contains `TZ: Asia/Shanghai`

- [ ] **Step 4: Rebuild and verify runtime time**

Run: `docker compose up -d --build app`
Expected: app container rebuilds successfully

Run: `docker compose exec -T app date '+%Y-%m-%d %H:%M:%S %Z %z'`
Expected: output ends with `+0800`
