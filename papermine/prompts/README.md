# Prompt 模板规范

- 每个 Agent 一个或多个 prompt 文件：`papermine/prompts/<agent>.md`
- 文件头必须含版本号，任何改动必须 bump：

  ```
  <!-- version: 1 -->
  ```

- 加载时把 `prompt_versions` 写入 `Dossier.meta`，用于可重放（engineering.md §1.2）。
- 约定：system 段写结构化指令 + 输出 schema 引用；user 段注入**脱敏后**的项目事实与证据。
