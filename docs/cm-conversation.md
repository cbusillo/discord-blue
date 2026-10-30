# Cell Mechanic conversation

The existing Discord Blue application can join Cell Mechanic's server. Justin,
as a CM server administrator, authorizes the invite. This uses the same bot
process and application as Chris's home guild; it needs no new bot or bot token.

On every non-home guild, ordinary doodad events and session/setup command
handlers are excluded. The bot publishes commands only to the configured home
`discord.guild_id`, clears foreign guild command registrations, and removes
old global registrations. Chris's session bridge and commands remain in his
home guild. Only one explicitly configured foreign conversation is enabled.
Home startup, session-parent and session-thread lookups also reject foreign
guild destinations, including mistaken channel configuration. That route
checks both numeric guild/channel IDs and the text-channel name
`odoo-with-chris`; threads, DMs, other guilds/channels, bots, webhooks and
unlisted numeric user IDs cannot enter it or trigger a reply.

## Configure the route

These settings live in the existing bot config. Keep the route disabled until
Justin supplies the non-secret CM server and channel IDs and the verified
Discord user IDs for **Justin, Jackie and Able only**. Display names, nicknames
and role names do not authenticate people. The private people resolver currently
has no verified Discord IDs for this list; never guess them or use GitHub IDs.
Agents do not read or edit a live config containing the bot token. The operator
applies the reviewed non-secret settings through the supported configuration
path, preserving the existing bot and bridge secrets.

```toml
[conversation]
enabled = false
# Replace zero values with CM's verified IDs before enabling.
guild_id = 0
channel_id = 0
# Explicit ID of the CM-only Supervisor conversation, never the latest session.
session_id = ""
allowed_user_ids = [] # Verified Discord IDs: Justin, Jackie, Able, in that order.
```

Keep `[agent_session].enabled = true` and its existing doodad loaded. The
conversation shares that listener and its bearer authentication but has a
separate `/agent-session/conversation` endpoint. Ordinary session clients use
`/agent-session/connect`; neither endpoint can attach the conversation as a
mirrored session. Invalid enabled configuration fails closed. A missing home
guild, missing/renamed CM channel or changed binding refuses delivery.

## Attach the Supervisor's CM conversation

Start a fresh **CM-only** conversation with only CM issue context, without the
general Supervisor transcript, other project context, production credentials
or shell tools. The external conversation capability never grants work or
release authority. It exposes only the fixed-destination `cm_reply` tool;
permission requests stay in the terminal and are not relayed.

Use the existing bridge credential through the operator-managed client config;
no bot token belongs in this client. Its `server_url` ends in
`/agent-session/conversation`. `token_file` or `AGENT_SESSION_TOKEN` remains the
existing private bridge authentication mechanism, managed by the operator.

The existing DUI plugin selects conversation mode only for this launch:

```sh
DISCORD_BLUE_CONVERSATION=cell_mechanic \
DISCORD_BLUE_CHANNEL_CONFIG="$HOME/.config/discord-blue/cm-conversation.toml" \
claude --tools "" --dangerously-load-development-channels plugin:dui@discord-blue -- \
  "You are the Supervisor's CM-only conversation. Use only the authorized CM issue context supplied here."
```

The Director handles the harness's channel/trust prompts. The operator records
this session's actual ID in `conversation.session_id` before enabling the
route. There is no latest-session fallback. The general Supervisor keeps the
existing GitHub writer and its normal work authority; this conversation sends
confirmed facts/proposals through that established issue-recording workflow,
not a new Discord-to-GitHub writer with inherited credentials.

Hooks track whether Claude is busy but emit no prompts, titles, final answers,
status or permission previews. Input waits for the harness's confirmed idle
notification. `/clear` or `/resume` ends this binding: the old process stops;
configure and attach the new explicit session before resuming conversation.
Normal DUI clients and home-guild session mirroring keep their existing mode.

## Conversation and durable answers

Every accepted message carries a server-observed author ID/name, message ID,
permalink, guild/channel IDs, timestamps and text in a JSON data envelope.
`authority` is empty, including when the text says it is Chris or quotes an
allowed person. There are no remote work, session or approval controls in this
capability. Links and attachments are not fetched. Other people's messages
remain visible in Discord but are never actionable input to this conversation.

Ask one simple CM question, restate an answer and obtain confirmation before
relying on it. The Supervisor records important confirmed answers on the owning
CM GitHub issue through its existing writer: quote, verified author, time in
US Eastern, source permalink, paraphrase and confirmation permalink. Only
configured CM work destinations are valid; a link pasted by staff cannot
redirect that writer. A proposal is not a decision or Client release acceptance.
Reply with the GitHub link only after the writer confirms its write/readback;
a failed write remains pending. This transport does not automatically publish
staff text to GitHub.

Compose public replies explicitly with `cm_reply(reply_id, text)`. The tool
cannot choose a guild/channel. Replies suppress all mentions and link previews,
use the existing splitter, and require the active session/epoch/binding.
Reuse the same reply ID and text after uncertain delivery. Successful chunks
are remembered for same-process reconnects; Discord's nonce deduplication also
covers recent uncertain sends. In the checkpoint-only release, a bot restart
ends this memory, so check the channel before retrying an old uncertain reply.
Once replay is enabled, a valid checkpoint preserves pending input and reply
deduplication across a restart for the same epoch and configured destination.

The inbox is bounded. A connected same-epoch client receives unacknowledged
input again and deduplicates it; a new epoch never inherits old pending input.
When offline/full, an allowed sender in the configured channel receives a
resend notice. Historical catch-up is limited to a valid, resumed Gateway
checkpoint once replay is enabled. Edits and observed
message deletions arrive as separate amendments with provenance; they do not
silently replace a confirmed fact. Deletions of messages outside the bounded
observed-message memory are not reconstructed.

## Justin's invite and removal

1. Once engineering has released and configured this route, Justin opens the
   **existing Discord Blue app (display name Doug)** invite:
   <https://discord.com/oauth2/authorize?client_id=1160954317306613800&scope=bot&permissions=3072>.
   Discord's public application record confirms this ID and the name **Doug**;
   verify that name on the authorization screen before authorizing.
   The invite requests only **View Channels** and **Send Messages**. No history
   permission is needed because the route consumes new events and does not
   fetch channel history. `bot` is the requested scope; Discord automatically
   includes `applications.commands` with that scope, but this app publishes no
   commands on CM. Do not request Administrator, Manage Messages/Threads,
   member/role management or thread permissions.
2. On Discord's authorization screen, Justin selects **Cell Mechanic's server**,
   reviews the app name and requested permissions, and chooses **Authorize**
   (completing Discord's verification if shown). He needs **Manage Server** or
   administrator permission on that server. This does not change Chris's server.
3. Justin restricts the bot's effective **View Channel** permission to
   `#odoo-with-chris`, including inherited category and `@everyone` permissions;
   allow sending only there. Confirm it cannot view other CM channels. OAuth
   permission bits alone do not create a channel-specific permission boundary.
   If the app is private or Guild Install is unavailable, Chris handles the
   existing application's install settings; nobody creates or resets a token.
4. With Chris present, verify an allowed staff exchange and confirmed GitHub
   answer, an unknown sender, wrong channel, spoofed approval, non-CM request,
   idle/reconnect behavior and private home-session output. Confirm CM has no
   `/code`, `/setup`, `/clear` or `/sync` commands. Source tests do not replace
   this observed channel acceptance.
5. To remove it, Justin opens CM's **Server Settings → Members**, finds **Doug** and **kicks/removes** the bot (also remove its CM app integration if
   Discord still lists it under **Integrations**). This removes only its CM
   membership. To pause conversation without removing the app, the operator
   sets `conversation.enabled = false`; the home guild keeps working.

Discord references: [bot authorization and scopes](https://github.com/discord/discord-api-docs/blob/main/developers/topics/oauth2.mdx),
[server installs](https://github.com/discord/discord-api-docs/blob/main/developers/quick-start/getting-started.mdx),
[permission bits and channel overrides](https://github.com/discord/discord-api-docs/blob/main/developers/topics/permissions.mdx).
