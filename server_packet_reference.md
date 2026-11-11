# YO3 Server Packet Reference

This reference documents the packet IDs on the server.

---

## 1. Packet structure

| Field | Meaning |
|---|---|
| `request` | Outer packet ID |
| `duelRequestName` | Duel subcommand inside a duel packet |
| `id` | Inner engine command ID when using engine-command subcommand `0x11` |
| `fields` | Named packet values such as player ID, command ID, position or index |
| `items` | Repeated/nested records, such as cards, decks, players or options |

---

## 2. Gate server packets

| Packet ID | Name | Direction | Details |
|---|---|---|---|
| `0x21FE` | Gate redirect | Server → Client | Provides Front server `host` and `port`. Sent after the Gate handshake. |

---

## 3. Front server packets

The following are incoming Front requests and the responses or actions the
server associates with them.

### 3.1 Login and player management

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x1F47` | Login/account details | `0x219A` | Returns account information including `regulation_id`, `user_id`, `uname` and `dpoint`. |
| `0x1F48` | Select language / initialize account | `0x2201`, push `0x23F0` | Reads language selection and initializes account/session information. |
| `0x1F49` | List account players | `0x2260` | Returns player records associated with the account. |
| `0x1F4A` | Create player | `0x2262` / `0x2263` | Creates a player using fields such as `pname` and `pindex`. |
| `0x1F4B` | Delete player | `0x2264` / `0x2265` | Deletes the selected player. |
| `0x1F4C` | Select player | `0x2266` | Selects the active player and initializes session data. |
| `0x2CEE` | Refresh player profile | `0x2CF1` | Refreshes current player information. |
| `0x2CEF` | Update player profile | `0x2CF3` | Updates profile data. |
| `0x2CF0` | Search players | `0x2CF5` | Returns matching player records. |
| `0x2CED` | Rate player | `0x2CF4` | Handles player rating operation. |

### 3.2 Decks and card collection

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x1F4E` | List decks | `0x22C8` | Returns saved decks. |
| `0x1F4F` | Upload/update deck | `0x22CA` / `0x22CB` | Receives deck data. Update type 1 includes `deck_id`, `deck_name`, `main`, `extra`, `side` and `card_protector`. |
| `0x1F50` | Delete deck | `0x22CC` | Deletes a saved deck. |
| `0x1FA4` | Card trunk/collection | `0x2328` | Returns card collection data. |
| `0x9000` | Card collection/startup dataset | `0x2328` | Used in the collection data path. |

### 3.3 Friends and ignore lists

| Request ID | Name | Reply / push | Details |
|---|---|---|---|
| `0x24B8` | List friends | `0x251C` | Returns friend records. |
| `0x24C1` | List ignored players | `0x252A` | Returns ignored-player records. |
| `0x24B9` | Send friend request | `0x2522` | Sends a friend request; may push `0x251F` to the target. |
| `0x24BB` | Accept/decline friend request | `0x2524` | Reads `to_id` and answer. |
| `0x24BD` | Cancel friend request | `0x2528` | Cancels an outgoing request. |
| `0x24BF` | Remove friend | `0x2529` | Removes a friend. |
| `0x24C2` | Add to ignore list | `0x252C` | Adds a player to the ignore list. |
| `0x24C3` | Remove from ignore list | `0x252E` | Removes a player from the ignore list. |
| `0x24C4` | Online compatibility/takeover | `0x2530` | Acknowledges a legacy startup request. |

Additional social packets:

| Packet ID | Name | Details |
|---|---|---|
| `0x251F` | Incoming friend request notification | Sent to the target player. |
| `0x251C` | Friend list | Friend-list response or refresh push. |
| `0x252A` | Ignore list | Ignore-list response or initialization push. |
| `0x2528` | Friend/request status | Used for request cancellation/status notification. |
| `0x2522` | Friend request result | Result of sending a request. |
| `0x2524` | Friend response result | Result of accepting or declining. |
| `0x2529` | Remove friend result | Result of removing a friend. |
| `0x252C` | Add ignore result | Result of adding an ignored player. |
| `0x252E` | Remove ignore result | Result of removing an ignored player. |

### 3.4 Mail

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x2583` | Mail count | `0x2588` | Returns mail counters. |
| `0x9603` | Mail count compatibility request | `0x9608` | Compatibility route to mail count handling. |
| `0x2582` | Fetch mail | `0x2586` | Returns mail data. |
| `0x2580` | Send mail | `0x2584` | Sends mail. |
| `0x2581` | Mail notification | Push | Notifies a recipient of mail. |

### 3.5 Chat rooms

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x2135` | List chat rooms | `0x2456` | Returns room records. |
| `0x2136` | Enter chat room | `0x2458` | Joins a room. |
| `0x2137` | Leave chat room | `0x245A` | Leaves a room. |
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
| `0x1B66` | Open shop | `0x1C20` | Returns shop welcome data. |
| `0x7014` | Shop compatibility request | `0x7200` | Returns shop welcome data. |
| `0x1B67` | Booster pack shelf | `0x1C22` | Returns booster pack items. |
| `0x1B68` | Buy shop item | `0x1C24` | Processes purchase and returns updated duel-point balance. |
| `0x1B69` | Avatar shelf | `0x1C2A` | Returns avatar catalogue. |
| `0x1B6B` | Protector/sleeve shelf | `0x1C32` | Returns protector and sleeve catalogue. |
| `0x1B6D` | Structure deck shelf | `0x1C2E` | Returns structure deck catalogue. |
| `0x1B6F` | Exchange shelf | `0x1C26` | Currently returns an empty item list. |
| `0x1B72` | Shop perk count | `0x1C36` | Currently returns `perk_num=0`. |
| `0x2C34` | Rental card list | `0x2D6C` | Currently returns an empty list. |
| `0x2C35` | Rental history | `0x2D6E` | Currently returns an empty history list. |
| `0x2C40` | Rental summary | `0x2D75` | Currently returns zero rental count/points/term. |
| `0x20D0` | Duel-point balance | Balance response | Returns current BP/duel-point balance. |

### 3.7 Regulation, events and NPC duels

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x2072` | Regulation list | `0x2398` | Returns 20 regulation IDs as packed big-endian `>20H` data. |
| `0x27D8` | Auxiliary startup dataset | `0x27D9` | Returns one empty record; client expects an item. |
| `0x2710` | NPC roster | Roster response | Builds NPC roster for selection scene. |
| `0x177A` | Event list | `0x177B` | Returns event data. |
| `0x1789` | Event list compatibility request | `0x177B` | Event-data request path. |
| `0x339A` | Event status | `0x33FE` | Currently returns `remain=0`, `event_id=0`. |
| `0x1793` | Championship result/list | `0x177B` | Currently returns `result_number=0`. |
| `0x19C8` | Duel logs | `0x19C9` | Currently returns an empty list. |
| `0x17FC` | Duel menu status | `0x17FD` | Returns duel state flags, currently zero. |
| `0x1784` | Begin NPC duel join | `0x1785` | Creates join context and returns join metadata. |
| `0x17AC` | Select NPC / duel metadata | `0x17AD` / `0x17AE` | Reads `cpu_id`; returns duel metadata or error. |
| `0x17D4` | Confirm NPC duel connection | `0x17D5` / `0x17D6` | Validates duel IDs and join state. |
| `0x1798` | Start automatic duel | `0x1839` | Creates an AI duel and sends connection announcement. |
| `0x17B6` | Host player challenge | `0x17B7` | Registers a challenge. |
| `0x17CA` | Accept/reject challenge | `0x1839` | On acceptance, sends connection information to both players. |
| `0x1838` | Start/announce duel | `0x1839`, `0x22C4` | Sends duel connection announcement and duel data. |
| `0x2775` | Fetch next present | `0x2777` / `0x2778` | Returns next present or empty result. |

### 3.8 Other Front requests

| Request ID | Name | Reply | Details |
|---|---|---|---|
| `0x1F41` | Fetch news | `0x21FC` | Returns one news item. |
| `0x271A` | Auxiliary operation | `0x271B` | Empty acknowledgement. |
| `0x10010` | Auxiliary operation | `0x10011` | Empty acknowledgement. |
| `0x2AFB` | Team information | Method-defined | Calls team information handler. |
| `0x11003` | Team information compatibility | Method-defined | Calls team information handler. |
| `0x2AFD` | Team member list | Method-defined | Calls team members handler. |
| `0x11005` | Team member compatibility | Method-defined | Calls team members handler. |
| `0x10101` | Auxiliary startup request | `0x10104` | Empty acknowledgement. |
| `0x17F2` | Auxiliary operation | `0x17F3` | Empty acknowledgement. |
| `0x17A2` | Cancel matchmaking | `0x17A3` | Empty acknowledgement. |
| `0x178E` | Auxiliary operation | `0x178F` | Empty acknowledgement. |
| `0x9500` | Auxiliary dataset request | `0x9501` | Empty dataset response. |
| `0x9514` | Auxiliary dataset request | `0x9515` | Empty dataset response. |
| `0x11628` | Auxiliary dataset request | `0x11629` | Empty dataset response. |
