# Code writer

Ask your bot for a script in plain words. The brain writes it and Code writer saves it as a file.

**It writes code but never runs it.** Read every script before you use it, and run it yourself only when you understand what it does.

## PuffPAI

### What it does

| Tool | What it does | Changes |
|---|---|---|
| `write_script` | The brain writes a script from your request in Python, PowerShell, Bash, JavaScript or HTML. It is saved as a file; you get its path and first lines. Never run. | Yes |
| `list_scripts` | The scripts written so far, newest first, with size and date. | No |
| `read_script` | The whole text of one saved script. | No |
| `delete_script` | Deletes one saved script. | Yes |

Things you might say: "write me a Python script that renames my photos by the date they were taken", "show me the photo renaming script", "delete the script rename-photos.py".

### What it needs

- The brain (text). When the brain is away, it says so in one sentence and saves nothing.
- No internet, no keys. About 0.2 GB of memory.

### Setup steps

None.

### Data

- Each script is one file in `made\` inside this package's own data folder. Nothing is written anywhere else.
- File names are cleaned (letters, digits and dashes), a name that is taken gets `-2`, `-3` and so on, and a script is at most 200 KB.
- On remove, PuffPAI asks whether to keep the scripts.

### How it runs

A small Python server (standard library only) on 127.0.0.1, started by PuffPAI. It answers MCP at `/mcp`, refuses any call without its PuffPAI token, and asks the brain only through the PuffPAI Core on this computer.

## Rough edges

- The code is only as good as the brain. It can be wrong, or do something you did not mean. That is why it is never run here: you read it first.
- No way to run or test a script from here. HomeBot had a separate sandbox for that; this package does not.
- It does not edit code you already have, and it does not read your files. Paste the code into your request if you want changes made to it.
- Only five languages: Python, PowerShell, Bash, JavaScript and HTML.
- When the brain answers without a code block, its whole answer is saved as the script, so read it.

Cypher Sisters. puff puff pass.
