# agent/security/

- pii_redactor.py：识别并脱敏手机号、邮箱等 PII，避免敏感信息进入日志/Trace。
- prompt_guard.py：识别 Prompt Injection 和不安全输入。

先看 prompt_guard.py 的输入检查，再看 pii_redactor.py 如何在观测链路中使用。安全模块不负责业务回答，也不负责 RAG。
