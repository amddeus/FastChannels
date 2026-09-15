"""
youtubetv.py — YouTube TV's own linear channel lineup (native app identity,
NOT Adobe Pass / TV-Everywhere — a different, unrelated integration from
YouTubeTV-as-MVPD, which only uses this same Google account to unlock other
networks' TVE gateways).

Auth: reuses the Google master_token already captured by the "Sign in with
Google" flow (app/tve/google_master_token.py + app/tve/browser_login/google.py)
that YouTubeTV-as-MVPD pairing depends on — same account, no separate
credential entry for this scraper. From that master_token,
mint_youtubetv_bearer_token() (gpsoauth.perform_oauth against the real
"YouTube TV" Android app identity, ANDROID_UNPLUGGED InnerTube client) mints a
short-lived Bearer token. Confirmed live 2026-09-15 that a plain OAuth
device-code Bearer token (the obvious first thing to try) is NOT accepted by
youtubei.googleapis.com's endpoints (400 INVALID_ARGUMENT) — it has to be this
gpsoauth-minted, YTTV-app-scoped one.

Channel discovery: youtubei.googleapis.com/youtubei/v1/unplugged/browse with
browseId=FEunplugged_epg, then following the grid's REAL pagination
continuation. This is NOT the "reload" continuation each sort-mode
(Default/Custom/A-Z/...) option carries in its own subMenuItems — that one
just reselects the sort mode and returns the same empty shell. The actual
per-channel rows come from a second field entirely:
contents.epgRenderer.paginationRenderer.epgPaginationRenderer
.continuations[0].nextContinuationData.continuation — confirmed live
2026-09-15 (236+ stationId rows on the first page alone, with a further
`continuations` entry for more). This diverges from a mirrored competitor
reference implementation (dev/dvrtuner, gitignored) that expected names/logos
inline on each row via `station.epgStationRenderer` — that shape is gone;
current rows are bare `stationId` (a YouTube channel ID) with nothing else.
Real per-channel names/Gracenote IDs come from `_STATION_CROSSWALK` below (a
baked-in static lookup, not a live call — see its own comment for
provenance/caveats) for 169 of the 236 live stations; the remaining 67 fall
back to the bare stationId as a placeholder name — a plain public
youtube.com/channel/<id> page does NOT work as a further fallback (confirmed:
these stationIds are internal-only entities with no real public channel
page).

EPG: fetch_epg() uses the SAME browseId=FEunplugged_epg call, but with an
`unpluggedBrowseOptions.epgOptions` body field the channel-discovery call above
deliberately omits — its presence is what determines whether the grid returns
bare stationId rows or each station's real forward schedule (title,
description, thumbnail, real beginTimeMs/endTimeMs). Found in a real web-client
HAR (dev/yttv/2.har, gitignored) and confirmed live 2026-09-15 through this
scraper's own ANDROID_UNPLUGGED auth — not a web-client-only trick. Paginates
up to _EPG_MAX_PAGES (~21h forward coverage); each page is a real ~1-1.5MB
authenticated call so this is deliberately capped well under the API's own
7-day max rather than exhausting it every scrape.

Playback: NOT implemented — resolve() raises. Why this is a much bigger lift
than every other DRM scraper in this codebase: the real web player delivers
video over SABR (a proprietary binary protocol — POST + protobuf framing,
response type application/vnd.yt-ump — not DASH/HLS Media3 can consume) and
fetches the Widevine license through a custom Innertube envelope
(player/get_drm_license), not a raw CDM-challenge passthrough. There IS an
unused-looking fallback in the player response (a plain dashManifestUrl +
manifest-embedded yt:SystemURL license hints — the exact simple shape every
other scraper here uses) but it was never reachable for a real test: this
account has no active YouTube TV subscription, so even an unrelated public
video came back UNPLAYABLE through this app identity — expected/subscription-
gated behavior, not a protocol failure. Revisit resolve() if/when a real
subscription is available to test the fallback against.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .base import BaseScraper, ChannelData, ProgramData
from .category_utils import category_for_channel
from ..models import TVEAccount
from ..tve import adobe_pass
from ..tve.google_master_token import mint_youtubetv_bearer_token

logger = logging.getLogger(__name__)

SCHEME = 'youtubetv://'
INNERTUBE_HOST = 'https://youtubei.googleapis.com/youtubei/v1'
CLIENT_CONTEXT = {
    'client': {
        'clientName': 'ANDROID_UNPLUGGED',
        'clientVersion': '8.24.0',
        'androidSdkVersion': 25,
        'hl': 'en',
        'gl': 'US',
        'deviceMake': 'samsung',
        'deviceModel': 'SM-T550',
        'osName': 'Android',
        'osVersion': '7.1.1',
        'platform': 'MOBILE',
    },
}
USER_AGENT = (
    'com.google.android.apps.youtube.unplugged/8.24.0'
    '(Linux; U; Android 7.1.1; en_US; SM-T550 Build/NMF26X) gzip'
)
_MAX_EPG_PAGES = 20  # sanity guard against an infinite continuation loop

# EPG schedule fetch options. Discovered from a real web-client HAR (dev/yttv/2.har,
# gitignored) -- WITHOUT this unpluggedBrowseOptions.epgOptions block, browseId=
# FEunplugged_epg returns bare stationId rows with no airings at all (that's the
# degraded shape channel discovery above works around). WITH it, the same call
# returns each station's forward schedule (title, description, thumbnail, real
# beginTimeMs/endTimeMs) -- confirmed live 2026-09-15 through this scraper's own
# ANDROID_UNPLUGGED auth, not just the web client. maxAiringsPerStation=18 and the
# duration values below are copied from the real web client's own request, which
# is the only place these numbers are documented.
_EPG_MAX_AIRINGS_PER_STATION = 18
_EPG_INITIAL_DURATION_MS = 15594000   # ~4.3 hours
_EPG_PAGINATION_DURATION_MS = 8502000  # ~2.4 hours per subsequent page
_EPG_MAX_DURATION_MS = '604800000'     # 7-day cap the API itself enforces
_EPG_MAX_PAGES = 8  # ~4.3h + 7*2.4h =~ 21h forward coverage; each page is a real
                     # ~1-1.5MB authenticated call, so this is deliberately capped
                     # well under the 7-day max rather than exhausting it every scrape.
_EPG_GAP_FILL_MAX_PAGES = 3  # supplemental now-playing pass, see fetch_epg()

# Static crosswalk: stationId -> (name, gracenote_id). Two sources, both
# resolved via videoDetails.channelId lookups (works even though playback
# itself is subscription-gated -- see module docstring):
# 1. A user-supplied channel list (name + tv.youtube.com/watch/<videoId> +
#    tvc_guide_stationid -- NOT committed, lived at dev/yttv/list.json,
#    gitignored). 143/148 resolved; 138 matched a real station.
# 2. The live FEunplugged_home "what's on now" feed's own LIVE-badged
#    items (no file needed -- self-sourced from the API). 83 unique live
#    videoIds fetched, 32 resolved to a channelId not already covered by #1;
#    a plain public youtube.com/channel/<id> page does NOT work as a
#    fallback for the rest -- confirmed these stationIds are internal-only
#    entities with no real public channel page (empty ytInitialData shell).
# Built 2026-09-15; 169 of 236 live stations covered, 67 still unnamed (a
# couple of entries above, e.g. the two "ESPN+" ids, come from #2 but didn't
# match any station in the live grid at capture time -- harmless, just dead
# weight in this dict, kept in case a future scrape's grid includes them).
# Gracenote IDs are from source #1 only, UNVERIFIED against an independent
# source (only 69 of these entries have one at all) -- treat as a best-effort
# starting point, not confirmed-accurate, until cross-checked.
_STATION_CROSSWALK: dict[str, dict[str, str | None]] = {
    'UC-HQSAeHQfSy-MEsqMIzJgA': {'name': 'OWN', 'gracenote_id': '70387'},
    'UC-lP-hsD59yeNhLV-cL266A': {'name': 'NewsNation', 'gracenote_id': '91096'},
    'UC-zVwDuCJxADjeftYF_YQyg': {'name': 'MGM+ Marquee', 'gracenote_id': None},
    'UC0ARTCKFnn6CNqBnrV3VLRg': {'name': 'FXM', 'gracenote_id': '14988'},
    'UC2HeknU5r0Gmr1a9Pz1O2Ug': {'name': 'Nick Jr.', 'gracenote_id': '105857'},
    'UC2NY4WS4K-rSwsDRHIKWKwg': {'name': 'JusticeCentral.TV', 'gracenote_id': None},
    'UC2k4QuNzyDDyuhpSX2PnlGw': {'name': 'HBO 2 East', 'gracenote_id': None},
    'UC2tIXKwzHcpgGFkNPqE9_nw': {'name': 'HBO Family East', 'gracenote_id': None},
    'UC2u6by_bSmjjFSThIktJSRQ': {'name': 'FXX', 'gracenote_id': '17927'},
    'UC2vtKivdDk-kjg_qTzZTzUg': {'name': 'Comedy.TV', 'gracenote_id': None},
    'UC2zFZSlhcDtpYBUr57eymPw': {'name': 'getTV', 'gracenote_id': None},
    'UC33MnhgSnxj6gi6u78KFsBg': {'name': 'LiveNOW from FOX', 'gracenote_id': None},
    'UC3A3AJWkUb8xZnlmL6ayOOQ': {'name': 'HSN', 'gracenote_id': '62077'},
    'UC3KZEZtc4Iz9A_x6ms5KMOA': {'name': 'Discovery Channel', 'gracenote_id': '56905'},
    'UC3UMF1UVLKV0-nHFIJRlVcA': {'name': 'Starz Encore East', 'gracenote_id': None},
    'UC3g9rzlm1aasACEwPMRV-5A': {'name': 'CMT', 'gracenote_id': '10138'},
    'UC3rkSU0RQVyFYK-roH4jOnA': {'name': 'AMC Thrillers', 'gracenote_id': None},
    'UC3vrYoaWRCZBVLZA3fePO7w': {'name': 'MovieMax', 'gracenote_id': None},
    'UC3yf-Rw0bg-iT0q57dhHkhA': {'name': 'Showtime 2', 'gracenote_id': None},
    'UC4C0FjxHuZcNMG8X3IuRtjA': {'name': 'MyOutdoorTV', 'gracenote_id': None},
    'UC4S7frnF1aFbe_zcygsYe3A': {'name': 'SYFY', 'gracenote_id': '24533'},
    'UC4WzX4rjxyOavZENjRCHEaw': {'name': 'TLC', 'gracenote_id': None},
    'UC7I_eOniDUFbPeESYDeL4DQ': {'name': 'Magnolia Network East', 'gracenote_id': '18544'},
    'UC82DH9XlBvAiTPPcGTWldaw': {'name': 'Starz Comedy', 'gracenote_id': None},
    'UC8B-kuFYd-TONdka6jxHWVg': {'name': 'OuterMax', 'gracenote_id': None},
    'UC8cihSzdMLhd7Z1LpHubbTQ': {'name': 'MTV2', 'gracenote_id': '16361'},
    'UC8e3UXoeR6ph1_DqY8lgK2w': {'name': 'GMA News TV International', 'gracenote_id': None},
    'UC8f_6NxAk-x_HqzrIHTem9w': {'name': 'FS1', 'gracenote_id': None},
    'UC8j-HIVNIgtewBryQ4o2k8A': {'name': 'SundanceTV', 'gracenote_id': None},
    'UC8lqGZa37U4OO610SMbyQJw': {'name': 'FOX SOUL', 'gracenote_id': '119212'},
    'UC9oV5ZyHOEfA93G7y-vsnVA': {'name': 'Cozi', 'gracenote_id': '112981'},
    'UCA6ODcHN12JiMJ-D8qOfW_w': {'name': 'Recipe.TV', 'gracenote_id': None},
    'UCAuZjoXwexKxzlVK9zGNRNw': {'name': 'HBO Zone East', 'gracenote_id': None},
    'UCB60DsHM5eD6FYUv_kw-7Ww': {'name': 'MGM+ East', 'gracenote_id': None},
    'UCBvMULs2YrbFjhPJQBKILhw': {'name': 'CNBC', 'gracenote_id': '10139'},
    'UCCYrcqAHdnJzkes2xe_X4Yw': {'name': 'Cartoon Network', 'gracenote_id': '60048'},
    'UCChSzsVWe1H3Kc2Fqn4atEQ': {'name': 'Go Channel', 'gracenote_id': '61854'},
    'UCCji-FUr16pJrPC0QGYX8NA': {'name': 'Starz Encore Classic', 'gracenote_id': None},
    'UCD2hzWipLHCVmwHux7oZbKw': {'name': 'Scripps News', 'gracenote_id': '96827'},
    'UCD9-oAYJYLAX5GAKCz_48SA': {'name': 'TBS', 'gracenote_id': '11867'},
    'UCDRP-3aMlEQ6uoYm_RKeCOg': {'name': 'Animal Planet', 'gracenote_id': '57394'},
    'UCDn_3JCrVk1CD8aBWvW-QQQ': {'name': 'TeenNick', 'gracenote_id': None},
    'UCEDGa3iCzYDSBCdC4iJU6xw': {'name': 'ShoxBET', 'gracenote_id': None},
    'UCE_hCgEokRm29XFFHusTKZw': {'name': 'Showtime Women', 'gracenote_id': None},
    'UCEfD9yuRsH9ZIqmnHHBJfqw': {'name': 'Showtime West', 'gracenote_id': None},
    'UCEo26zV6dzpkFvEd1408olw': {'name': 'Nickelodeon', 'gracenote_id': None},
    'UCEyNozLxr4I7rZimVumfVHg': {'name': 'Starz Encore West', 'gracenote_id': None},
    'UCFSR1oVlgsOSiQe4dPnBOfw': {'name': 'BET Her', 'gracenote_id': '97358'},
    'UCFb9FWb-U9DKNwfTeCnamTw': {'name': 'Cinemax', 'gracenote_id': None},
    'UCFehJp_rl3lm8-8OHoqO3gw': {'name': 'Smithsonian Channel', 'gracenote_id': '65799'},
    'UCFsj5mvKOU6gEkbaptDHc3w': {'name': 'FS2', 'gracenote_id': None},
    'UCGbypKg9TspCXLL3yYzz31g': {'name': 'Disney XD', 'gracenote_id': '60006'},
    'UCHS5Yi6DGuW8ljpK8fd8Vsw': {'name': 'Stadium', 'gracenote_id': None},
    'UCI-EfxFyywB_IrXCyUgLYow': {'name': 'Dabl', 'gracenote_id': '112976'},
    'UCIMG7wOFkuGnuo5ZwwU08-A': {'name': 'ScreenPix', 'gracenote_id': None},
    'UCIVWabtxQrh8o2lzEu4EvBw': {'name': 'MTV Classic', 'gracenote_id': '59054'},
    'UCIZnLsx7JgR0b47tsmO9j1g': {'name': 'Paramount', 'gracenote_id': '59186'},
    'UCIfeUa31arBgYR1GuDzMR6w': {'name': 'VH1', 'gracenote_id': None},
    'UCJ1bMGjLxUVz_uQL-3UyvlA': {'name': 'Start TV', 'gracenote_id': '109758'},
    'UCJ5alUuWAQAZMwDJf5XMUvg': {'name': 'Willow Cricket HD', 'gracenote_id': None},
    'UCJCcn5FyhJeu718vX-7jnJQ': {'name': 'CBS Sports Network', 'gracenote_id': None},
    'UCJDNzpdVXySLwZPI5GwhTNQ': {'name': 'MotorTrend', 'gracenote_id': '111101'},
    'UCKFToknzO8tSIK6YsCt9-QA': {'name': 'Cars.TV', 'gracenote_id': None},
    'UCKJIDt5EnyjGcuvluFCWOJQ': {'name': 'Tennis Channel', 'gracenote_id': None},
    'UCKmS_J820Nlwo9RvcOvdoSA': {'name': 'CineMax Latino', 'gracenote_id': None},
    'UCLzVssMTD4F7XUR1sh3JZMA': {'name': 'FOX Business', 'gracenote_id': '58718'},
    'UCM1eVv3qUHJa9fIwvWH1Cbg': {'name': 'ESPN+', 'gracenote_id': None},
    'UCMrLeKNQLIcj2l1k4yqkILQ': {'name': 'TNT', 'gracenote_id': None},
    'UCNFMyKDGUWEWD-xO3bQT3Hg': {'name': 'Bounce', 'gracenote_id': '55314'},
    'UCNd6UJFGnUUjllgfXMz_Y7A': {'name': 'TUDN', 'gracenote_id': None},
    'UCNp8TLrPZ6VB52E6tjqgu7w': {'name': 'NFL Network', 'gracenote_id': None},
    'UCO0DprpzkgRs0zpSGxMI6bw': {'name': 'Travel Channel', 'gracenote_id': None},
    'UCOU8W2EKGsLD10F2Vujo40w': {'name': 'Starz In Black', 'gracenote_id': None},
    'UCOjTLqTl1gVybbm5iOT9o9g': {'name': 'FOX Weather', 'gracenote_id': '123194'},
    'UCOtYFLYvZEiaZw4YpSjOndw': {'name': 'ESPN2', 'gracenote_id': '12444'},
    'UCP52ybr2d5kw2FwHZiKtR8g': {'name': 'PlayersTV', 'gracenote_id': None},
    'UCP7l2eYJokMkMQVwPs8WB_A': {'name': 'FX', 'gracenote_id': '14321'},
    'UCPIvHJrgKn3AWMhkMA8EoPQ': {'name': 'HLN', 'gracenote_id': '64549'},
    'UCPMoZoTUinLM-MngPHWijUw': {'name': 'MSNBC', 'gracenote_id': '16300'},
    'UCQBq9yc19hqHFxMhFLDcXCA': {'name': 'MTV', 'gracenote_id': '10986'},
    'UCQRRBYgUQJ_EhwqS_dAy33w': {'name': 'Pickleball TV', 'gracenote_id': None},
    'UCR9hPhAxagm_0FdbX_1UCoQ': {'name': 'HBO Comedy East', 'gracenote_id': None},
    'UCRQcfaq4UbB7EisWRXD-9xA': {'name': 'Nat Geo', 'gracenote_id': '143571'},
    'UCRUaN9crBc_JInWlxOVuymA': {'name': 'Starz Edge', 'gracenote_id': None},
    'UCRXsF67RpQTpfgrHdCKoOSA': {'name': 'WE tv', 'gracenote_id': None},
    'UCRiIubRNtAUghYKB0_KFtGQ': {'name': 'Court TV', 'gracenote_id': '111043'},
    'UCRmDpn3FghrsHZ567Z4qUHA': {'name': 'Fight Network', 'gracenote_id': None},
    'UCRpa59GI1QwGkXq2V0kSCVQ': {'name': 'Nicktoons', 'gracenote_id': None},
    'UCSEgGARD80sIB05c-YyC7xw': {'name': 'ESPN+', 'gracenote_id': None},
    'UCSRDwPEZ5tkrRx_oXYX4o_Q': {'name': 'NBC News NOW', 'gracenote_id': '114174'},
    'UCStfhR2V58QkCCyq_8dlk6g': {'name': 'CNN', 'gracenote_id': '10142'},
    'UCSw1UZ2F4Ee95QbCRLtY6DA': {'name': 'beIN Sports XTRA', 'gracenote_id': None},
    'UCUKPIBgCHBAi7dbVeZv-GxA': {'name': 'ActionMax East', 'gracenote_id': None},
    'UCULL3PM8sg7l7yAEocqkZ6w': {'name': 'Cheddar News', 'gracenote_id': '107241'},
    'UCV2pXl1pkayqpxL8KWY6zfg': {'name': 'All Reality WE tv', 'gracenote_id': None},
    'UCV6aAjNXFAq4sM8NHO35wKA': {'name': 'Starz Encore Espanol', 'gracenote_id': None},
    'UCVN4FmqZlqjiNT9uR48Ll1Q': {'name': 'Disney Channel', 'gracenote_id': '59684'},
    'UCVQ9StUJS6wzebqN7KRgxkQ': {'name': 'MGM+ Drive-In', 'gracenote_id': None},
    'UCW7W_WAogi3qWDbO9PqOmZQ': {'name': 'ESPN', 'gracenote_id': '10179'},
    'UCWBR8qGJOBAe5PJXOeMLB0w': {'name': 'T2', 'gracenote_id': None},
    'UCX6uEhY1FuU3yz7if36XlYA': {'name': 'Paramount+ with SHOWTIME', 'gracenote_id': None},
    'UCXgV84lI09EqItQYLxCrZ0w': {'name': 'Portlandia', 'gracenote_id': None},
    'UCYLG3atJfxvRoVjsH_4zQqg': {'name': 'theGRIO', 'gracenote_id': None},
    'UCYMkTuztF5_bV8nTUxzKzwg': {'name': 'MyOutdoorTV', 'gracenote_id': None},
    'UCYcks1y0PKWWkhQBMkf5uMA': {'name': 'Starz Encore Action', 'gracenote_id': None},
    'UCYdjAW3r89cZr6m-nFGXkjg': {'name': 'PokerGO+', 'gracenote_id': None},
    'UCZaTc6ohEtA1uNyWD_5-O8Q': {'name': 'ID', 'gracenote_id': '16615'},
    'UCZwxosConLtVUgnydqL08dQ': {'name': 'HBO East', 'gracenote_id': None},
    'UC_iYDoUH42IulhrKQnUdLgw': {'name': 'Localish', 'gracenote_id': None},
    'UCaPfU4OoKvm-bRrsay4T4qw': {'name': 'Starz Encore Westerns', 'gracenote_id': None},
    'UCaY_dmTBpOG9kWfXq-VLgtQ': {'name': 'CHARGE!', 'gracenote_id': '91578'},
    'UCawPT_7asTNbNwt-0s8P8lw': {'name': 'OutsideTV', 'gracenote_id': None},
    'UCazXZSdH_UXDGvmSrtdfxuA': {'name': 'Starz Kids & Family', 'gracenote_id': None},
    'UCb8Prbrv7Xghfdoc-jlejug': {'name': 'One America News', 'gracenote_id': None},
    'UCbE0fBz-3rt6UW0mBvoAD7g': {'name': 'Starz Cinema', 'gracenote_id': None},
    'UCbFaRGY8Bw3eLldVIFPDYhw': {'name': 'Bravo', 'gracenote_id': '10057'},
    'UCbjVsshw_-h8ZRQBYAacA2Q': {'name': 'AMC', 'gracenote_id': '10021'},
    'UCbji8ZLXWwsBOrw6VQB7IgQ': {'name': 'ION', 'gracenote_id': '18633'},
    'UCcADVqJNXUHEypmdHJHdvZw': {'name': 'ThrillerMax East', 'gracenote_id': None},
    'UCcHKBCoR3g3jorhhRgdrjRg': {'name': 'The Weather Channel', 'gracenote_id': None},
    'UCcZNGZM43Gfl67_nGQjVZ-A': {'name': 'Pets.TV', 'gracenote_id': None},
    'UCdj7clRJstl32igUku_7Jaw': {'name': 'ACC Network', 'gracenote_id': '124806'},
    'UCdloZ69KU3lzw9mlNeJwsXA': {'name': 'Showtime Family Zone', 'gracenote_id': None},
    'UCe12hHZUXsNfelyfz9xg0nw': {'name': 'Overtime', 'gracenote_id': None},
    'UCed9LOJZCWonC04hDB3H7xQ': {'name': 'Bloomberg Television', 'gracenote_id': None},
    'UCevSLUtapg5P80Eu2WIDzdg': {'name': 'Nat Geo Wild', 'gracenote_id': None},
    'UCfMg7kRbNcYIpTRhWBmRAtw': {'name': 'ScreenPix Action', 'gracenote_id': None},
    'UCgAaOBjobOPnRRACWlMn30w': {'name': 'BET', 'gracenote_id': '10051'},
    'UCgLwjiTwZRuiiEfvZXjSZBw': {'name': 'TYT Network', 'gracenote_id': None},
    'UCguDPkNwA5sSy3C3upoKGDg': {'name': 'IFC', 'gracenote_id': '14873'},
    'UChAbpGpJzppYHFhHflFvTQg': {'name': 'ABC News Live', 'gracenote_id': '113380'},
    'UChukTzuQDPENsI9IYCvDEaA': {'name': 'ESPNews', 'gracenote_id': '16485'},
    'UCi2WWF4AMSF8vvNjMrBvDgw': {'name': 'Hallmark Drama', 'gracenote_id': '105723'},
    'UCiG6vqJXFHxaLfcyvy_0CHw': {'name': 'HBO Signature East', 'gracenote_id': None},
    'UCiiV9Ifjzxq1R0VhMs8DmOw': {'name': 'Stories by AMC', 'gracenote_id': None},
    'UCik6qJnVdzpvs5cShUCASbw': {'name': 'Starz Encore Suspense', 'gracenote_id': None},
    'UCjeeSYEak0yoYOPXtKlLjOQ': {'name': 'NBA TV', 'gracenote_id': '32382'},
    'UCjwQspjtRywSoImMpdFiaXw': {'name': 'BBC News', 'gracenote_id': '89542'},
    'UCk1VlyRGl0MblDM9X1_h7zg': {'name': 'Willow Xtra HD', 'gracenote_id': None},
    'UCko2QKmtBtKuh6KoKVDZWtA': {'name': 'TV Land', 'gracenote_id': None},
    'UCkteTgPnQlVGOEqd6Qp6-iw': {'name': 'MavTV', 'gracenote_id': None},
    'UCl7SHXEgznwaw_X0jsZFEcg': {'name': 'TBD TV', 'gracenote_id': None},
    'UClJKvIp41p37bGJOPWEO2Og': {'name': 'NEWSMAX', 'gracenote_id': None},
    'UClLiyqVgOVfuZxaKUHSCLgQ': {'name': 'MGM+ Hits', 'gracenote_id': None},
    'UClQPfdwgE1sF5CWYLXGctEg': {'name': '5StarMax', 'gracenote_id': None},
    'UClxYtgBSuQzBdpIsGHXJx5g': {'name': 'HGTV', 'gracenote_id': '49788'},
    'UCmBskGxtl-7At_lMzyLJYKA': {'name': 'BTN', 'gracenote_id': None},
    'UCmaPhYJOxFSETzGlJzZytpA': {'name': 'QVC', 'gracenote_id': '60222'},
    'UCmzl5oTI37uvXbAv_UN9NNQ': {'name': 'Game Show Network', 'gracenote_id': None},
    'UCnkxpCddkyNVVt5VCXHXw-g': {'name': 'MoreMax', 'gracenote_id': None},
    'UComo7105e_nqubnARiG7hjA': {'name': 'Starz Encore Family', 'gracenote_id': None},
    'UCp5PQrmM0FmLtn_ZCSrL8jg': {'name': 'Starz West', 'gracenote_id': None},
    'UCp9TSEVSOM5TrZR1xyftSDQ': {'name': 'truTV', 'gracenote_id': None},
    'UCpdAKAdxFbMGoZ4ufO-HHnw': {'name': 'HBCU GO', 'gracenote_id': None},
    'UCpj5ieQ9I6GFSsRnON4AOBQ': {'name': 'SEC Network', 'gracenote_id': '89714'},
    'UCqWYIzQLZFW4NFXPfWBLW1w': {'name': 'FOX News', 'gracenote_id': '60179'},
    'UCqhyiwRnWi8k8BVuffvoRSQ': {'name': 'Starz Encore Black', 'gracenote_id': None},
    'UCrxOU69oUq5Pb8oneCtTIyQ': {'name': 'Hallmark Channel', 'gracenote_id': '66268'},
    'UCsIuB1rNY2tATlP1XjSbCvg': {'name': 'Fox4K', 'gracenote_id': None},
    'UCtGP7XtIzd3bwARBmJNcZYA': {'name': 'ESPNU', 'gracenote_id': '45654'},
    'UCumdadB3EZECXspnGutJNEg': {'name': 'Showtime Extreme', 'gracenote_id': None},
    'UCv15E33qNOnHUKi188QZ7Aw': {'name': 'BBC America', 'gracenote_id': '18332'},
    'UCv_pZhnIyyPj3jf9TS9uToQ': {'name': 'Freeform', 'gracenote_id': '59615'},
    'UCvaTleka5KrZxTWcmFFUpjg': {'name': 'ScreenPix Westerns', 'gracenote_id': None},
    'UCvd5mBfn5QSez1f6i4F-dQg': {'name': 'Showtime East', 'gracenote_id': None},
    'UCvtIF-MwpMBXKsbYZvuHpYg': {'name': 'Tastemade', 'gracenote_id': '107076'},
    'UCvx0APUm4CuRszt9BRNA9TA': {'name': 'FOX Soccer Plus', 'gracenote_id': None},
    'UCwCx1zNO1Qd4BHq3Jjl064Q': {'name': 'Disney Junior', 'gracenote_id': '75004'},
    'UCwOohtHXo0hkQMB4WO3Ib8g': {'name': 'Food Network', 'gracenote_id': '12574'},
    'UCwUoTw92Bghbi86Mua1jM6Q': {'name': 'Starz East', 'gracenote_id': None},
    'UCwig0jo9FYVff261Pd05MgQ': {'name': 'Comedy Central', 'gracenote_id': '64599'},
    'UCwmtfDGiiipObmIEi37e9hg': {'name': 'Hallmark Movies & Mysteries', 'gracenote_id': '46710'},
    'UCxXjdrevcsy6xh0_O3KhokA': {'name': 'USA', 'gracenote_id': '11208'},
    'UCxlgoUf5jJaGXbH_GaD4G5Q': {'name': 'HBO Latino East', 'gracenote_id': None},
    'UCyeti25JXK1zbcL33QtJ2zg': {'name': 'NBCLX', 'gracenote_id': None},
}


class YouTubeTVAuthError(RuntimeError):
    pass


def channel_id_for_url(raw_url: str) -> str | None:
    if not raw_url or not raw_url.startswith(SCHEME):
        return None
    return raw_url[len(SCHEME):]


class YouTubeTVScraper(BaseScraper):
    """YouTube TV's own linear channel lineup. See module docstring — channel/
    EPG discovery only, playback intentionally not implemented yet."""

    source_name = 'youtubetv'
    display_name = 'YouTube TV'
    source_category = 'premium'
    is_premium = True
    config_required = True  # needs the shared Google sign-in (Settings -> TVE) done first
    under_development = True  # channel/EPG discovery only, no playback yet
    scrape_interval = 720
    stream_audit_enabled = False  # nothing resolvable to audit yet

    def _bearer_token(self) -> str:
        account = TVEAccount.query.filter_by(provider_id='mvpd').first()
        if not account:
            raise YouTubeTVAuthError('TVE account not found — save TVE settings first.')
        saved = adobe_pass.load_google_master_token(account)
        if not saved:
            raise YouTubeTVAuthError(
                'No Google sign-in on file — use "Sign in with Google" in Settings -> TVE first.')
        token = mint_youtubetv_bearer_token(saved)
        if not token:
            raise YouTubeTVAuthError(
                'Google sign-in on file could not mint a YouTube TV token '
                '(revoked or expired) — sign in with Google again.')
        return token

    def _api_call(self, bearer: str, endpoint: str, body: dict) -> dict:
        payload = dict(body)
        payload['context'] = CLIENT_CONTEXT
        resp = self.session.post(
            f'{INNERTUBE_HOST}/{endpoint}', json=payload,
            headers={
                'Authorization': f'Bearer {bearer}',
                'Content-Type': 'application/json',
                'User-Agent': USER_AGENT,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_station_ids(self, bearer: str) -> list[str]:
        """Returns the deduped list of stationId values (YouTube channel IDs)
        across the whole paginated EPG grid. See module docstring for why the
        continuation token is NOT the one attached to each sort-mode option."""
        first = self._api_call(bearer, 'unplugged/browse', {'browseId': 'FEunplugged_epg'})
        pag = (
            first.get('contents', {}).get('epgRenderer', {})
            .get('paginationRenderer', {}).get('epgPaginationRenderer', {})
        )
        continuations = pag.get('continuations') or []
        cont_token = (
            continuations[0].get('nextContinuationData', {}).get('continuation')
            if continuations else None
        )
        if not cont_token:
            logger.warning('[youtubetv] no EPG pagination continuation in first response')
            return []

        station_ids: list[str] = []
        seen: set[str] = set()
        pages = 0
        while cont_token and pages < _MAX_EPG_PAGES:
            pages += 1
            page = self._api_call(bearer, 'unplugged/browse', {'continuation': cont_token})
            pag_contents = page.get('continuationContents', {}).get('epgPaginationRenderer', {})
            added = 0
            for row in pag_contents.get('contents', []):
                sid = row.get('epgRowRenderer', {}).get('stationId')
                if sid and sid not in seen:
                    seen.add(sid)
                    station_ids.append(sid)
                    added += 1
            next_conts = pag_contents.get('continuations') or []
            next_token = (
                next_conts[0].get('nextContinuationData', {}).get('continuation')
                if next_conts else None
            )
            # A page that added nothing new means the "next" token is a fixed
            # point (observed live 2026-09-15: it can point back to the same
            # snapshot rather than advancing) — stop instead of re-fetching
            # identical data until the safety cap trips.
            if added == 0 or next_token == cont_token:
                break
            cont_token = next_token
        if pages >= _MAX_EPG_PAGES:
            logger.warning('[youtubetv] EPG grid pagination hit the %d-page safety cap', _MAX_EPG_PAGES)
        return station_ids

    def fetch_channels(self) -> list[ChannelData]:
        bearer = self._bearer_token()
        station_ids = self._fetch_station_ids(bearer)
        if not station_ids:
            logger.warning('[youtubetv] EPG grid returned no channels')
            return []
        matched = sum(1 for sid in station_ids if sid in _STATION_CROSSWALK)
        logger.info('[youtubetv] %d/%d stations matched the static name/gracenote crosswalk',
                     matched, len(station_ids))

        channels = []
        for sid in station_ids:
            known = _STATION_CROSSWALK.get(sid)
            # No real name/logo comes back from the live grid itself (see
            # module docstring) — fall back to the bare stationId for
            # anything the crosswalk doesn't cover.
            name = known['name'] if known else sid
            channels.append(ChannelData(
                source_channel_id=sid,
                name=name,
                slug=f'youtubetv-{sid.lower()}',
                stream_url=f'{SCHEME}{sid}',
                stream_type='hls',
                category=category_for_channel(name, None, self.source_name) if known else None,
                language='en',
                country='US',
                gracenote_id=known.get('gracenote_id') if known else None,
            ))
        return channels

    def fetch_epg(self, channels: list[ChannelData], **kwargs) -> list[ProgramData]:
        wanted = {ch.source_channel_id for ch in channels}
        bearer = self._bearer_token()

        # Two passes, merged (dedup on (station, start_time, title) below):
        # 1. Broad pass, same duration values the real web client uses -- full
        #    236-station breadth, ~21h forward coverage. BUT the first page it
        #    returns starts ~_EPG_INITIAL_DURATION_MS (~4.3h) after "now" for
        #    most stations, not at "now" itself -- confirmed live 2026-09-15 by
        #    varying initialEpgFetchDurationMs and watching where each
        #    station's first returned airing lands; not a pagination-loop bug,
        #    the very first real page already starts that late.
        # 2. Small gap-fill pass (initialEpgFetchDurationMs=0) specifically to
        #    catch the currently-airing program. This does NOT reach all 236
        #    stations no matter how many pages are fetched (confirmed: 3 pages
        #    and 8 pages both topped out around 94-95/236) -- appears to be a
        #    real characteristic of this endpoint, not a page-count limit, so
        #    a handful of channels will legitimately show only their next
        #    upcoming airing instead of what's on right now.
        programs, pages1 = self._fetch_epg_pass(bearer, wanted, _EPG_INITIAL_DURATION_MS, _EPG_MAX_PAGES)
        gap_fill, pages2 = self._fetch_epg_pass(bearer, wanted, 0, _EPG_GAP_FILL_MAX_PAGES)

        seen: set[tuple] = set()
        merged: list[ProgramData] = []
        for program in programs + gap_fill:
            key = (program.source_channel_id, program.start_time, program.title)
            if key in seen:
                continue
            seen.add(key)
            merged.append(program)

        now = datetime.now(timezone.utc)
        by_channel: dict[str, list[ProgramData]] = {}
        for p in merged:
            by_channel.setdefault(p.source_channel_id, []).append(p)
        with_now = sum(
            1 for progs in by_channel.values() if any(p.start_time <= now <= p.end_time for p in progs)
        )
        logger.info(
            '[youtubetv] fetched %d programs across %d stations (%d+%d pages); '
            '%d/%d stations have now-playing coverage',
            len(merged), len(by_channel), pages1, pages2, with_now, len(by_channel),
        )
        return merged

    def _fetch_epg_pass(
        self, bearer: str, wanted: set[str], initial_duration_ms: int, max_pages: int,
    ) -> tuple[list[ProgramData], int]:
        body = {
            'browseId': 'FEunplugged_epg',
            'unpluggedBrowseOptions': {
                'epgOptions': {
                    'maxAiringsPerStation': _EPG_MAX_AIRINGS_PER_STATION,
                    'initialEpgFetchStartTimeMs': str(int(time.time() * 1000)),
                    'initialEpgFetchDurationMs': initial_duration_ms,
                    'paginationDurationMs': _EPG_PAGINATION_DURATION_MS,
                    'maxDurationMs': _EPG_MAX_DURATION_MS,
                },
            },
        }
        first = self._api_call(bearer, 'unplugged/browse', body)
        pag = (
            first.get('contents', {}).get('epgRenderer', {})
            .get('paginationRenderer', {}).get('epgPaginationRenderer', {})
        )
        continuations = pag.get('continuations') or []
        cont_token = (
            continuations[0].get('nextContinuationData', {}).get('continuation')
            if continuations else None
        )
        if not cont_token:
            logger.warning('[youtubetv] no EPG pagination continuation for schedule fetch (duration=%d)',
                            initial_duration_ms)
            return [], 0

        programs: list[ProgramData] = []
        seen: set[tuple] = set()
        pages = 0
        while cont_token and pages < max_pages:
            pages += 1
            page = self._api_call(bearer, 'unplugged/browse', {'continuation': cont_token})
            pag_contents = page.get('continuationContents', {}).get('epgPaginationRenderer', {})
            added = 0
            for row in pag_contents.get('contents', []):
                row_data = row.get('epgRowRenderer', {})
                sid = row_data.get('stationId')
                if sid not in wanted:
                    continue
                for airing in row_data.get('airings', []):
                    program = self._parse_airing(sid, airing.get('epgAiringRenderer', {}))
                    if not program:
                        continue
                    key = (sid, program.start_time, program.title)
                    if key in seen:
                        continue
                    seen.add(key)
                    programs.append(program)
                    added += 1
            next_conts = pag_contents.get('continuations') or []
            next_token = (
                next_conts[0].get('nextContinuationData', {}).get('continuation')
                if next_conts else None
            )
            if added == 0 or next_token == cont_token:
                break
            cont_token = next_token
        return programs, pages

    @staticmethod
    def _parse_airing(station_id: str, airing: dict) -> ProgramData | None:
        begin_ms = airing.get('beginTimeMs')
        end_ms = airing.get('endTimeMs')
        title_runs = airing.get('title', {}).get('runs', [])
        if not (begin_ms and end_ms and title_runs):
            return None
        title = title_runs[0].get('text', '').strip()
        if not title:
            return None
        try:
            start = datetime.fromtimestamp(int(begin_ms) / 1000, tz=timezone.utc)
            end = datetime.fromtimestamp(int(end_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            return None
        desc_runs = airing.get('quaternaryText', {}).get('runs', [])
        description = desc_runs[0].get('text', '').strip() if desc_runs else None
        thumbs = airing.get('thumbnail', {}).get('thumbnails', [])
        poster_url = thumbs[-1]['url'] if thumbs else None
        if poster_url and poster_url.startswith('//'):
            poster_url = 'https:' + poster_url
        video_id = airing.get('videoId')
        return ProgramData(
            source_channel_id=station_id,
            title=title,
            start_time=start,
            end_time=end,
            description=description,
            poster_url=poster_url,
            is_live=True,
            episode_id=video_id,
        )

    def resolve(self, raw_url: str) -> str:
        raise NotImplementedError(
            'YouTube TV playback is not implemented yet (channel/EPG discovery '
            'only) — see the youtubetv.py module docstring for why (SABR video '
            'delivery + a wrapped Widevine license envelope, and no subscription '
            'available yet to test the simpler unused fallback against).')
