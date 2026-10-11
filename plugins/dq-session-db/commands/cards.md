---
description: Control session summary cards (status/on/off/haiku/local/model/key/test/now)
argument-hint: "[status|on|off|haiku|claude MODEL|local URL MODEL [KEY_ENV]|model NAME|url URL|key-env VAR|key KEY|thinking on|off|test|now [SESSION]|disable]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/cards-ctl.sh:*)
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/cards-ctl.sh $ARGUMENTS` (use `status` when no
arguments were given) then:

Reply with the script's output exactly as it printed it, inside a code block: same
lines, same words, nothing summarized or rephrased, and nothing added before or after.

Pass arguments through unchanged. Put single quotes around any argument containing `${`,
so a URL like `${MY_LLM_URL}/v1` is stored as written and expanded at call time.

`now` and `test` call the configured model and can take a minute or more on a long session.

If the arguments contain an API key (`key ...`), do not repeat the key in your reply.

Do not explain the plugin, do not suggest follow-up commands, and do not run anything else.
