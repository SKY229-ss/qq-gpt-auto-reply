# QQ GPT Auto Reply

A local desktop application that connects a QQ bot through Tencent's official Python SDK and uses the official Codex CLI to draft text replies. This is a community project, not an official Tencent or OpenAI product.

**Supported conversations: private messages sent to the bot and group messages explicitly mentioning the bot. This application cannot read your personal QQ inbox or observe replies you send in the personal QQ app.** Automatic replies are off by default. Live QQ message delivery has not yet been verified for this project.

## Reply rules

The requested workflow is recorded in [`workflow.json`](workflow.json):

- **Private chats:** when someone messages the user, reply after 60 seconds only if the human has not replied.
- **Group chats:** apply that rule only when the human user or bot is explicitly mentioned. Ignore all other group messages.
- **Attribution:** every automatic reply ends with `(Written by GPT)`.
- **History:** user-supplied or exported history may be read only to imitate speaking style. Never edit or delete history, or use it to infer whether the human has replied.
- **Settings:** do not change unrelated QQ settings.

The live adapter implements a narrower, explicitly selected mode. It receives bot-addressed private messages and official group @bot events. It does not receive group messages that mention only the human. Human replies are observable only when sent through this application's control window, using the bot identity. The full personal-account workflow above is therefore not implemented.

Enable automatic mode only if you will use the control window for human replies in these bot conversations. A reply made elsewhere cannot reliably cancel a pending reply here. No unofficial personal-account automation is included.

## Requirements

- macOS or Linux with Python 3.10 or later and Tk support.
- A QQ bot application with an AppID and AppSecret from [QQ Open Platform](https://q.qq.com/).
- The official Codex CLI installed and available as `codex`, with a normal saved login. Follow the [official Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
- An internet connection and a computer that stays awake while the application is running.

Bot availability, permitted users, groups, and verification requirements are controlled by QQ Open Platform. Check your bot's dashboard before testing with other people.

The GPT bridge uses Codex CLI feature flags to restrict generation to text. These flags may depend on the installed CLI version. Compatibility with every CLI version is not guaranteed; generation errors withhold replies.

## Set up

From this project folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x "Launch QQ Bot.command"
.venv/bin/python desktop.py
```

On macOS, after creating `.venv` and installing the requirements, you can also open `Launch QQ Bot.command`.

1. Create or open your bot in [QQ Open Platform](https://q.qq.com/) and obtain its AppID and AppSecret.
2. Enter them in the local control window and select **Connect QQ**. Enter the secret only in the masked field; do not commit it or share it in an issue.
3. Send a private message to the bot in QQ, or explicitly @mention the bot in a group where it is available. The message should appear in the control window.
4. Select the message, choose **Draft with GPT**, review the draft, and choose **Send my reply**. Confirm that it arrives in QQ before relying on the integration.
5. If the supported control-window mode suits your use, select **Use this window for human replies and enable 60-second auto replies**. It applies to new messages; messages received while paused are not released automatically.

AppSecret remains in process memory and is used for Tencent's official bot authentication. This application does not save it. Closing the window disconnects the bot; enter the credentials again when reopening. To switch to another bot AppID, close and reopen the window.

## Timing and delivery

The timer begins at the first unanswered eligible message. Follow-up messages from the same person are combined without extending it. Group participants have separate timers. Generation starts when a reply is due, so the actual delivery occurs after 60 seconds plus generation and network time.

A successful human reply through the panel cancels the corresponding pending automatic reply. The application checks human-reply status before generation and again before sending. Pausing, disconnecting, closing the window, or an unresponsive control window suppresses automatic sends. Reconnecting leaves automatic mode paused. A send already committed cannot be retracted by a later human action.

The application appends `(Written by GPT)` outside the model and preserves it when sending GPT drafts. Human-authored manual replies do not receive that note. Sends are limited to the adapter's supported QQ reply window. GPT failures do not cause an endless retry loop. Uncertain QQ deliveries are not blindly retried; check QQ before sending again.

This is a local desktop application, not a hosted service. Keep the window open and the computer awake.

## Data and credentials

- Up to 200 message entries are held in memory. The application does not write message text to its delivery database.
- `.qq-state/<app_id>/` contains the application's local state for each bot, including delivery identifiers, statuses, and timestamps for duplicate protection. The entire `.qq-state/` directory is ignored by Git.
- QQ AppSecret is not saved by the application. AppID is used to separate local delivery metadata. Codex manages its own saved login; this application does not read or copy authentication tokens.
- Eligible message content is sent to the configured Codex service to generate replies, subject to that service's terms and usage allowance.
- Incoming text is treated as untrusted message data. It cannot choose recipients, change timing, alter settings, or remove the final GPT note.
- The GPT bridge requests an ephemeral, read-only session with user configuration, project instructions, and tool features disabled. Unsupported CLI options or unexpected output cause generation to fail rather than sending a reply.
- No history importer, personal QQ history reader, or cross-conversation memory is included. A future style profile must follow the read-only, style-only history rule.

## Validation

Run the offline tests from this project folder:

```sh
.venv/bin/python -m unittest test_rules test_live
```

The suite covers timing, explicit mention filtering, human cancellation, pause and disconnect behavior, duplicate suppression, conversation separation, uncertain delivery, and attribution. Offline tests do not verify QQ authentication, account permissions, or actual message delivery. Perform the manual round-trip check above with your own bot.

## Files

| File | Purpose |
| --- | --- |
| `desktop.py` | Native control window and local service lifecycle |
| `live.py` | Tencent SDK adapter and manual/automatic reply delivery |
| `gpt_bridge.py` | Restricted text generation through the Codex CLI |
| `rules.py` | Trigger, timing, cancellation, and attribution rules |
| `test_rules.py`, `test_live.py` | Offline behavior checks |
| `workflow.json` | Requested semantics and supported live scope |
| `reply-instructions.txt` | Instructions for generating reply text |
| `Launch QQ Bot.command` | macOS launcher using the project's `.venv` |

## Official references

- [Tencent QQ SDK](https://github.com/tencent-connect/botpy)
- [Tencent private-message example](https://github.com/tencent-connect/botpy/blob/master/examples/demo_c2c_reply_text.py)
- [Tencent group @bot example](https://github.com/tencent-connect/botpy/blob/master/examples/demo_group_reply_text.py)
- [Codex non-interactive mode and saved authentication](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
