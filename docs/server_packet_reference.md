# YGO3 Server Packet Reference

This document describes the packet IDs and routes implemented by the Python YO3
server. It is based on the `handle_front` dispatcher and related handlers in
`ygo3_server.py`.

A route listed here is evidence that the server recognizes the request; it does
not necessarily mean the feature is fully implemented or that every client
workflow has been tested. Where a handler is a placeholder, returns empty data,
or has method-defined responses, that limitation is called out.

---

## 1. Packet structure

| Field | Meaning |
|---|---|
| `request` | Outer packet ID. |
| `duelRequestName` | Duel subcommand inside a duel packet. |
| `id` | Inner engine command ID when using engine-command subcommand `0x11`. |
| `fields` | Named packet values such as player ID, command ID, position or index. |
| `items` | Repeated/nested records, such as cards, decks, players or options. |

Packet IDs below are written in hexadecimal. “Reply” identifies the response
packet explicitly sent by the server; “push” identifies an unsolicited
notification. A handler may send more than one packet or no packet, depending
on state.

---

## 2. Gate server packets

| Packet ID | Name | Direction | Details |
|---|---|---|---|
| `0x21FE` | Gate redirect | Server → Client | Provides the Front server `host` and `port` after the Gate handshake. |

---

## 3. Front server packets

The following are incoming Front requests and the responses or actions the
server associates with them.

### 3.1 Login and player management

| Request ID | Name | Reply / push | Details |
|---|---|---|---|
| `0x1F47` | Login/account details | `0x219A` | Returns `regulation_id`, `user_id`, `uname` and `dpoint`. |
| `0x1F48` | Select language / initialize account | `0x2201`, push `0x23F0` | Reads `lang`, sets the session country, returns account/player summary and sends Duel Points/premium status. The premium push uses `dpauth_result=0`. |
| `0x1F49` | List account players | `0x2260` | Returns player records associated with the account. |
| `0x1F4A` | Create player | Handler-defined (`0x2262` / `0x2263`) | Creates a player using fields such as `pname` and `pindex`. |
| `0x1F4B` | Delete player | Handler-defined (`0x2264` / `0x2265`) | Deletes the selected player. |
| `0x1F4C` | Select player | Handler-defined (`0x2266`) | Selects the active player and initializes session data. |
| `0x2CEE` | Refresh player profile | Handler-defined | Re-sends the selected player's record. |
| `0x2CEF` | Update player profile | Handler-defined | Saves profile edits. |
| `0x2CF0` | Search players | Handler-defined | Returns matching player records. |
| `0x2CED` | Rate player | Handler-defined | Handles manner/rating operation for an opponent. |

Requests `0x2CEE`, `0x2CEF`, `0x2CF0`, `0x2CED` and most requests after
character selection are ignored until a player has been selected.

### 3.2 Decks and card collection

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x1F4E` | List decks | `0x22C8` | Returns saved decks. |
| `0x1F4F` | Upload/update deck | `0x22CA` / `0x22CB`; may also create NPC duel state | Receives deck data. Update type 1 includes `deck_id`, `deck_name`, `main`, `extra`, `side` and `card_protector`. |
| `0x1F50` | Delete deck | `0x22CC` | Deletes a saved deck. |
| `0x1FA4` | Card trunk/collection | `0x2328` | Returns card collection data. |
| `0x9000` | Card collection compatibility route | `0x2328` | Uses the same trunk handler as `0x1FA4`. |

### 3.3 Friends and ignore lists

| Request ID | Name | Reply / push | Details |
|---|---|---|---|
| `0x24B8` | List friends | `0x251C` | Returns the friend list. |
| `0x24C1` | List ignored players | `0x252A` | Returns the ignore list. |
| `0x24B9` | Send friend request | `0x2522`; may push `0x251F` | Sends a friend request and may notify the target. |
| `0x24BB` | Accept/decline friend request | `0x2524` | Reads target ID and answer. |
| `0x24BD` | Cancel friend request | `0x2528` | Cancels an outgoing request. |
| `0x24BF` | Remove friend | `0x2529` | Removes a friend. |
| `0x24C2` | Add to ignore list | `0x252C` | Adds a player to the ignore list. |
| `0x24C3` | Remove from ignore list | `0x252E` | Removes a player from the ignore list. |
| `0x24C4` | Online compatibility/takeover | `0x2530` | Acknowledges a legacy startup request. |

Additional social response/push IDs:

| Packet ID | Name | Details |
|---|---|---|
| `0x251F` | Incoming friend request notification | May be pushed to the target player. |
| `0x251C` | Friend list | Friend-list response or refresh push. |
| `0x252A` | Ignore list | Ignore-list response or initialization push. |
| `0x2522` | Friend request result | Result of sending a request. |
| `0x2524` | Friend response result | Result of accepting or declining. |
| `0x2528` | Friend/request status | Used for cancellation/status notification. |
| `0x2529` | Remove friend result | Result of removing a friend. |
| `0x252C` | Add ignore result | Result of adding an ignored player. |
| `0x252E` | Remove ignore result | Result of removing an ignored player. |

### 3.4 Mail

| Request ID | Name | Reply / push | Details |
|---|---|---|---|
| `0x2583` | Mail count | Handler-defined (`0x2588`) | Returns mail counters. |
| `0x9603` | Mail count compatibility request | Handler-defined (`0x2588` or compatibility path) | Uses the mail-count handler. |
| `0x2582` | Fetch mail | `0x2586` | Returns mail data and clears fetched mail as implemented by the handler. |
| `0x2580` | Send mail | `0x2581` | Sends mail; `0x2581` is the configured acknowledgement/notification route. |

### 3.5 Chat rooms

| Request ID | Name | Reply / push | Details |
|---|---|---|---|
| `0x2135` | List chat rooms | `0x2456` | Returns room records. |
| `0x2136` | Enter chat room | `0x2458` and possible membership updates | Joins a room. |
| `0x2137` | Leave chat room | `0x245A` and possible membership updates | Leaves a room. |
| `0x2134` | Send chat message | `0x2455` | Sends a chat line for room broadcast. |

Chat response and push IDs:

| Packet ID | Name | Details |
|---|---|---|
| `0x2455` | Chat line broadcast | Carries a chat line. |
| `0x2456` | Chat room list | Room-list response. |
| `0x2457` | Room membership update | Room status/membership notification. |
| `0x2458` | Enter-room result | Result of joining a room. |
| `0x245A` | Leave-room result | Result of leaving a room. |
| `0x245B` | Room status update | Room lifecycle notification. |
| `0x245C` | Room status update | Additional room lifecycle notification. |

### 3.6 Shop and inventory

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x1B66` | Open shop | `0x1C20` | Returns shop welcome text. |
| `0x7014` | Shop compatibility request | `0x7200` | Returns the same welcome text through a compatibility route. |
| `0x1B67` | Booster pack shelf | Handler-defined (`0x1C22`) | Returns booster pack items. |
| `0x1B68` | Buy shop item | Handler-defined (`0x1C24` and balance update) | Processes a purchase and updates Duel Points. |
| `0x1B69` | Avatar shelf | Handler-defined (`0x1C2A`) | Returns avatar catalogue. |
| `0x1B6B` | Protector/sleeve shelf | Handler-defined (`0x1C32`) | Returns protector and sleeve catalogue. |
| `0x1B6D` | Structure deck shelf | Handler-defined (`0x1C2E`) | Returns structure deck catalogue. |
| `0x1B6F` | Exchange shelf | `0x1C26` | Currently returns an empty item list. |
| `0x1B72` | Shop perk count | `0x1C36` | Currently returns `perk_num=0`. |
| `0x2C34` | Rental card list | `0x2D6C` | Returns current rental records. |
| `0x2C35` | Rental history | `0x2D6E` | Returns rental history. |
| `0x2C40` | Rental summary | `0x2D75` | Currently returns zero rental count/points/term. |
| `0x20D0` | Duel-point balance | `0x23F0` | Returns current Duel Points and premium authorization fields. |

### 3.7 Regulation, events and NPC duels

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x2072` | Regulation list | `0x2398` | Returns 20 regulation IDs as packed big-endian `>20H` data. |
| `0x27D8` | Auxiliary card-release dataset | `0x27D9` | Returns one empty record; client expects an item. |
| `0x2710` | NPC roster | Handler-defined | Builds NPC roster for the selection scene. |
| `0x177A` | Event list | `0x177B` | Returns configured event data. |
| `0x1789` | Event search compatibility/route | Handler-defined | Calls event search handler. |
| `0x339A` | Event status / own event | Handler-defined | Calls the user's event handler; not the `0x33FE` status route described in older drafts. |
| `0x1793` | Championship result/list | `0x177B` | Currently returns `result_number=0`. |
| `0x19C8` | Duel logs | `0x19C9` | Currently returns an empty list. |
| `0x17FC` | Duel menu status | `0x17FD` | Returns `duel_playing=0`, `duel_ready=0`, `duel_auto=0`. |
| `0x1784` | Begin NPC duel/event join | `0x1785` | Creates a join context and returns `join_id`, `server_id`, `event_score`, `duel_password_flag`; echoes `event_id` for the configured NPC event or a user event. |
| `0x17AC` | Select NPC / duel metadata | `0x17AD` / `0x17AE` | Reads `cpu_id`; returns duel metadata on valid join state, otherwise error `0x17AE`. |
| `0x17D4` | Confirm NPC duel connection | `0x17D5` / `0x17D6` | Validates join ID, duel ID and native duel state. On success, acknowledges and announces duel server. |
| `0x1798` | Start automatic duel | Connection packet / `0x1839` announcement | Placeholder pairing path; creates a duel only when the matchmaker is enabled. |
| `0x17B6` | Host player challenge | `0x17B7` | Registers a challenge when the matchmaker is enabled. |
| `0x17CA` | Accept/reject challenge | Connection packet; may push `0x1839` | On accepted challenge, sends connection information to the participants. |
| `0x1838` | Start/re-announce duel | Duel announcement and duel data | Announces the duel server. |
| `0x2775` | Fetch next present | `0x2777` / `0x2778` | Returns the next present or an empty result. |

### 3.8 Trade and rental scene requests

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x2BC0` | Invite to trade | Handler-defined | Creates a trade-scene invitation. |
| `0x2BC4` | Cancel trade invitation | Handler-defined | Cancels a trade invitation. |
| `0x2BC2` | Answer trade invitation | Handler-defined | Accepts/declines a trade invitation. |
| `0x2BD5` | Trade scene ready | Handler-defined | Marks the participant ready. |
| `0x2BC6` | Submit trade offer | Handler-defined | Updates the offer. |
| `0x2BC7` | Trade offer seen | Handler-defined | Acknowledges receipt/viewing of the offer. |
| `0x2BC8` | Trade status | Handler-defined | Updates trade status. |
| `0x2BC9` | Confirm trade | Handler-defined | Confirms the trade. |
| `0x2BCB` | Leave trade scene | Handler-defined | Leaves the trade scene. |
| `0x2C24` | Invite to rental scene | Handler-defined | Creates a rental-scene invitation. |
| `0x2C28` | Cancel rental invitation | Handler-defined | Cancels a rental invitation. |
| `0x2C26` | Answer rental invitation | Handler-defined | Accepts/declines a rental invitation. |
| `0x2C3E` | Rental scene ready | Handler-defined | Marks the participant ready. |
| `0x2C2B` | Set rental conditions | Handler-defined | Updates rental conditions. |
| `0x2C2A` | Set rental cards | Handler-defined | Updates selected rental cards. |
| `0x2C2C` | Rental offer/conditions seen | Handler-defined | Acknowledges received rental data. |
| `0x2C2D` | Confirm rental | Handler-defined | Confirms the rental. |
| `0x2C31` | Withdraw rental | Handler-defined | Withdraws the rental request. |
| `0x2C2F` | Answer rental invitation | Handler-defined | Processes the invitation answer. |
| `0x2C33` | Leave rental scene | Handler-defined | Leaves the rental scene. |
| `0x2D6C` | Rental records compatibility route | Handler-defined | Uses the rental-records handler. |

### 3.9 Teams

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x2AFB`, `0x11003` | Team information | Handler-defined | Both IDs call the team-information handler. |
| `0x2AFD`, `0x11005` | Team member list | Handler-defined | Both IDs call the team-members handler. |
| `0x2AF9` | Form team | Handler-defined | Creates/forms a team. |
| `0x2AFE` | Invite team member | Handler-defined | Sends a team invitation. |
| `0x2B02` | Cancel team invitation | Handler-defined | Cancels a team invitation. |
| `0x2B00` | Answer team invitation | Handler-defined | Accepts/declines a team invitation. |
| `0x2AFA` | Disband team | Handler-defined | Requests team disbandment. |
| `0x2B05` | Remove team member | Handler-defined | Removes a member. |
| `0x2B04` | Leave team | Handler-defined | Leaves the team. |
| `0x2B06` | Change team authority | Handler-defined | Changes member authority. |

### 3.10 User-created events and event lobby

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x339B` | Create event form | Handler-defined | Returns event creation form data. |
| `0x3390` | Create event | Handler-defined | Creates a user event. |
| `0x3397` | Enter event | Handler-defined | Enters a user event. |
| `0x178E` | Leave event | `0x178F` / handler-defined | Leaves an event. |
| `0x177F` | Event lobby information | Handler-defined | Returns event lobby information. |
| `0x3395` | Start event | Handler-defined | Starts an event. |
| `0x3393` | Cancel event | Handler-defined | Cancels an event. |
| `0x32CA`, `0x32CC`, `0x32CD`, `0x32CE`, `0x32C9`, `0x32D5`, `0x32CB` | Event lobby query/actions | Handler-defined | Routed to the event-lobby query handler; exact response depends on request ID and state. |

### 3.11 Other Front requests and compatibility acknowledgements

| Request ID | Name | Reply / action | Details |
|---|---|---|---|
| `0x2724` | Keep-alive ping | No reply | Explicitly ignored by the Front dispatcher. |
| `0x1F41` | Fetch news | `0x21FC` | Returns one item with `lang="1"`. |
| `0x271A` | Auxiliary operation | `0x271B` | Empty acknowledgement. |
| `0x10010` | Auxiliary operation | `0x10011` | Empty acknowledgement. |
| `0x10101` | Auxiliary startup request | `0x10104` | Empty acknowledgement. |
| `0x17F2` | Auxiliary operation | `0x17F3` | Empty acknowledgement. |
| `0x17A2` | Cancel matchmaking | `0x17A3` | Empty acknowledgement. |
| `0x9500` | Auxiliary dataset request | `0x9501` | Empty push/dataset response. |
| `0x9514` | Auxiliary dataset request | `0x9515` | Empty push/dataset response. |
| `0x11628` | Auxiliary dataset request | `0x11629` | Empty push/dataset response. |
| `0x251C` | Friend-list refresh request | Push `0x251C` | Sends an empty friend-list push in this compatibility route. |
| `0x252A` | Ignore-list refresh request | Push `0x252A` | Sends an empty ignore-list push in this compatibility route. |

Unknown Front request IDs are logged as unhandled. Requests that require a
selected player are ignored before player selection, except for the initial
login/player-selection routes and the keep-alive route.

---

## 4. Duel server packets

Duel traffic uses the outer duel packet and the `duelRequestName` subcommand.
The exact engine prompt/response formats are handled by the duel protocol
parser and response builders.

| `duelRequestName` | Name | Direction | Details |
|---|---|---|---|
| `0x11` | Engine command | Client → Server | Carries an inner engine command `id` and its fields. |
| `0x12` | Duel data / engine output | Server → Client | Carries engine messages and duel state updates. |
| `0x14` | Duel timer | Server → Client | Sends timer state. The server configures a 180-second turn timer and a 600-second overall game timer. |
| `0x15` | Duel connection/data notification | Server → Client | Used by the server's duel announcement/data path. |

The server maps engine messages such as `NEW_TURN`, `NEW_PHASE`, summon,
chain, attack, battle, damage, and win messages into the client's duel data
stream. Engine message IDs are not Front request IDs.

---

## 5. NPC/CPU duel engine behaviour

NPC duels are run through the native OCG engine. The engine produces legal
battle commands in `SELECT_BATTLECMD`; the server translates a selected battle
command into an engine response. The server-side CPU decision logic prefers a
direct attack when one is legal and otherwise selects an available attack
against an opposing monster. If no attack is available, it advances or ends
the Battle Phase according to the engine prompt.

---

## 6. Implementation notes and limitations

- This reference documents the Python server's current dispatch and handlers,
  not every packet understood by the original game client or the upstream
  YO3 protocol.
- “Handler-defined” means the request is routed to a method whose response
  may depend on account state, scene state, or request fields. Consult that
  method for the exact packet(s).
- Empty-list/zero-value responses are deliberate placeholders where noted;
  they should not be interpreted as full feature support.
- The server logs unhandled Front request IDs. Those logs are useful when
  extending this reference or investigating a client request not listed here.
