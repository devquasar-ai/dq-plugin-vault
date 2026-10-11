# DevQuasar skills

Free, local-first skills and plugins for AI coding agents, by [DevQuasar](https://devquasar.com).

| Plugin | What it does |
|---|---|
| [dq-session-db](plugins/dq-session-db/) | Your Claude Code history, kept and searchable. Find what was said or decided in any past session, see which files and commits it touched, and resume it, even after Claude Code has deleted the transcript. Optional summary cards from a model you choose. |
| [dq-local-tts](plugins/dq-local-tts/) | Hear Claude's replies, spoken entirely on your machine. macOS voices out of the box; Kokoro voices on any OS. No cloud, no API key. |

## Install

In Claude Code:

```
/plugin marketplace add devquasar-ai/skills
/plugin install dq-session-db@devquasar
/plugin install dq-local-tts@devquasar
```

Each plugin's README covers setup and commands. (DQ = DevQuasar; the prefix keeps the commands
from colliding with other plugins.)

## Other agents

The skills follow the open [Agent Skills](https://agentskills.io) format (`SKILL.md`), which
many agents read. The packaging here (hooks, slash commands, the marketplace) is Claude Code's.
session-db's command-line tool runs anywhere Python does, but today it reads Claude Code's
transcripts.

## More from DevQuasar

- 🤗 [huggingface.co/DevQuasar](https://huggingface.co/DevQuasar): 900+ quantized open-weight models
- 📝 [devquasar.com](https://devquasar.com): hands-on posts on local AI, hardware and models

MIT license. Issues and pull requests welcome.
