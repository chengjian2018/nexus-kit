# skills/ — nexus-kit 的技能资产约定根

这是 settings `skills.dir` 的默认扫描根（相对服务启动目录）。每个直接子目录
是一个技能：

```
skills/
  <skill-name>/          # 目录名即技能名（canonical name）
    SKILL.md             # 必需。YAML frontmatter: name / description /
                         #   requires_toolsets（可选，声明执行面依赖）
                         #   + metadata（自由 dict，如 version）
    references/ ...      # 可选。手册引用的参考文件，运行期经
                         #   read_skill_file(name, rel_path) 读取
```

机制要点（详见 `nexus/skills.py` 模块 docstring 与 `ARCHITECTURE.md`）：

- **无注册表、无 reload**：目录放进本根，mtime 指纹缓存发现变更后下一轮
  对话自动可用。
- **双层 deny-by-default**：pattern `allow_skills`（池）∩ node `use_skills`
  （启用），都空 = 无技能。
- **技能给知识不给权限**：生效集非空时 `default_loop` 自动追加只读工具
  `load_skill` / `read_skill_file`，并在 system prompt 注入每技能一行的
  元数据；手册指引下的脚本执行仍走 `bash` 等执行面工具的三层授权收口。
- `requires_toolsets` 声明的执行面依赖必须在 pattern `allow_toolset` 内，
  注册期 `validate_skills` fail-fast。

兼容 Claude Code 布局：`local_config.yaml` 里把 `skills.dir` 指向
`~/.claude/skills`，现有技能零改动即可被任何声明了它的节点使用
（参考 `apps/archify_skill_agent/`）。
