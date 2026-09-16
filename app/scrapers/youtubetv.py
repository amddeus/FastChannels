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
Real per-channel names/logos: fetch_channels() tries a LIVE lookup first
(_fetch_live_station_info()) — replicates the real web client's own
authenticated browseId=FEunplugged_epg + epgOptions call directly via
requests, using cookies read from the signed-in guide profile
(_YOUTUBETV_GUIDE_PROFILE_DIR, populated by
app/tve/browser_login/youtubetv_guide.py's run_youtubetv_guide_signin) plus a
locally-computed SAPISIDHASH — the same auth scheme the browser's own JS
computes for itself, not anything defeating a protection (it's this
account's own already-authorized session). No browser render needed, ~3-5s
total; confirmed live 2026-09-16 returning all 238/238 stations fresh. Falls
back to `_STATION_CROSSWALK` below (a baked-in static snapshot from the same
technique, one-off, 2026-09-16) on ANY failure — no guide sign-in yet,
expired session, profile busy with another browser-login job, network error
— so a scrape never hard-fails over this, it just risks slightly stale
names/logos on that one run. Both paths use the same fallback chain real
clients use for the name itself (name.runs -> callSign.runs ->
icon/secondaryIcon accessibility label) for stations whose `name` field is
empty in this response shape. A plain public youtube.com/channel/<id> page
does NOT work as a resolution method on its own (confirmed: these stationIds
are internal-only entities with no real public channel page). Gracenote IDs
have no live source at all — always come from the static crosswalk (a
user-supplied list, see its own comment) and still only cover a subset.

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

import hashlib
import logging
import time
from datetime import datetime, timezone

from .base import BaseScraper, ChannelData, ProgramData
from .category_utils import category_for_channel
from ..extensions import db
from ..models import Source, TVEAccount
from ..tve import adobe_pass
from ..tve.google_master_token import mint_youtubetv_bearer_token

logger = logging.getLogger(__name__)

SCHEME = 'youtubetv://'
INNERTUBE_HOST = 'https://youtubei.googleapis.com/youtubei/v1'
# Same path as _YOUTUBETV_ISOLATED_PROFILE_DIR in app/tve/browser_login/common.py
# -- duplicated as a literal here rather than imported, so this module doesn't
# pull in browser_login's heavier import chain just for one path string.
_YOUTUBETV_GUIDE_PROFILE_DIR = '/data/browser_profiles/youtubetv'
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

# Static crosswalk: stationId -> (name, gracenote_id, logo_url). Rebuilt
# 2026-09-16 from a REAL signed-in tv.youtube.com guide session (the
# youtubetv_guide sign-in flow -- app/tve/browser_login/youtubetv_guide.py)
# by capturing the browser's own natural unplugged/browse network response
# while it rendered the live guide -- not a one-off resolve-by-videoId, the
# actual station.epgStationRenderer data every real client gets. 238/238 live
# stations covered (100%), superseding the old partial (169/236) crosswalk
# built from a user-supplied list + videoId resolution. Also caught several
# real, current rebrands the old data had wrong or missing entirely (e.g.
# getTV -> GREAT, MSNBC -> MS NOW, MyOutdoorTV -> Huntstream, "Go Channel"
# was actually wrong -- real name is Golf Channel).
# Gracenote IDs are carried over from the prior crosswalk where a stationId
# matched (that data's provenance/caveats are unchanged: from a user-supplied
# list, UNVERIFIED against an independent source -- see git history for the
# original comment). logo_url is new, straight from the same capture.
_STATION_CROSSWALK: dict[str, dict[str, str | None]] = {
    'UC-8tJSqRlLmw3kv6-JJFzNg': {'name': 'ViX Premium Deportes 6', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bt6rpHsYXKMBxFKLwYNPTtfj12NrspjV2SgV7n-yQf64ahyBeQ4SNu9HMvaMUM6IqljaQRazZQuI=ns-nd'},
    'UC-GUAFqNiSCAMh5xrHnH2pA': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UC-HQSAeHQfSy-MEsqMIzJgA': {'name': 'OWN', 'gracenote_id': '70387', 'logo_url': 'https://yt3.ggpht.com/cs48EGOPtttUsscPZVZlXOop8JpY2kZqIgpSrsRpkkgidFrLZx0frElCML56hadAaHyaA3CQm-4=ns-nd'},
    'UC-SlJznb4Ei0SauGBqVXaFg': {'name': 'Bloomberg TV+ 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/U34YLUpE8OaE50xKniojZwM0I31UiZAS76xpuy-7f3QOsNQvQbz6NdTyw2Hg261_dC6iie7ZKWfz=ns-nd'},
    'UC-WzeEGMf7BdZjdTqPQ9_yA': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UC-lP-hsD59yeNhLV-cL266A': {'name': 'NewsNation', 'gracenote_id': '91096', 'logo_url': 'https://yt3.ggpht.com/aomxAvMoWX1xYEiVIaOe9nm_cCppreHY9wyP4ByMVoYWbVgcy1Izc-9VRZ4qOtgxiiLCE85lhZR6=ns-nd'},
    'UC-vAlo5UjEpcogmLxJB7xAA': {'name': 'NBC Sports 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/FBKN7TPvkgkYn91liABnNBenqj3NRUeh9G8nkEFH_5_ga69GO2Zl66B4xAmn00nzXNy1W4FQLFEO=ns-nd'},
    'UC-zVwDuCJxADjeftYF_YQyg': {'name': 'MGM+ Marquee', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/hiV1m3YK_ILYiuyo3T1VsQ5aIyKmoZRkqcJhzYurTVXmwFPULezUG0Rq-I9hHE1bimki2JZXfDH-3g=ns-nd'},
    'UC0ARTCKFnn6CNqBnrV3VLRg': {'name': 'FXM', 'gracenote_id': '14988', 'logo_url': 'https://yt3.ggpht.com/-ciiAYu52E16j9n2x9kA5Y7hIx1p4_oF7jzNAJjnjNVfThXbRlC8JsSAu5AelhJjh-1jqOwdEALV=ns-nd'},
    'UC0C6ulcDWfv75p--gIpk6dw': {'name': 'GMA Life TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/WRzQV2sv06btlU1Ry33Fz6KqSxmhvmBeUHJjrdn_QIXCgZwrKHMz1fHZKz-DR6gvWUo20sr-cIOl=ns-nd'},
    'UC0pCltT2BMAmsAVPk1uP17g': {'name': 'Law & Crime', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/DR7fglkZHGxxPi1yOJxtmXlZBxFPzTsDB5hk7ixtAFJ1zvRdK0jqjkH2fVm34jNCGA-b4hlZYjhm=ns-nd'},
    'UC0z7SePPnxw28MmK77GxB6A': {'name': 'Zee Punjabi', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/JFejgYNIWSFuXtjVaruEhKBTc2zzPYWhRNrWGo1MvnkUCzlbOET0KVrq6O_8t9DJDMXkv_zT6EGg=ns-nd'},
    'UC1dcLpSDzqDZwj2zldGBO1A': {'name': 'Adult Swim', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/-EbnPK70YLRbWyt-_3NKUqsrmmK_6Dsmw7Z_507GJCi0rWIJeDjrNZxfuC5Tm68zGLubU2hB4uMS=ns-nd'},
    'UC20b9aWEZVnvT7XNzkFMxlA': {'name': 'The Filipino Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bReBOWPK17Cv2FErtTlVFkmnRWllR3k4EdfBnvw72Wl1mLHi2elxc_D7sZLYEMnsAG4IX2ixrk20uw=ns-nd'},
    'UC2HeknU5r0Gmr1a9Pz1O2Ug': {'name': 'Nick Jr.', 'gracenote_id': '105857', 'logo_url': 'https://yt3.ggpht.com/yCAUMMC8zAshsY7PvolDNWF8WfEJfuGuk66crlqdPLeyXhHTqYO_M326ofMIMzOSZ6fCvh_0Tcar=ns-nd'},
    'UC2NY4WS4K-rSwsDRHIKWKwg': {'name': 'JusticeCentral.TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/HZUK8hwf6Lz_VmYxcDrq2qmQJwA3rDvQWY0C1nM6gSMOKHhuGg5-i9tNbRa-rk_q2i0zEkPfX8U-_Q=ns-nd'},
    'UC2k4QuNzyDDyuhpSX2PnlGw': {'name': 'HBO Hits East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/CEPD7tAi6qRFwLKy6B339_V9dNkqt9Wjrbn4LiodtG1riq-P0groc0Z_db7q5k9KgCaghboZYtMn=ns-nd'},
    'UC2tza2d01CFaPEYsFxYoqvQ': {'name': 'NBC Sports Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/xCTmggRwUTEQALfdi53K_thP9_Sywxz_-vVFCnvCIriGV7c2ytZO-iRSmnXyEouB679PQMR9yJo=ns-nd'},
    'UC2u6by_bSmjjFSThIktJSRQ': {'name': 'FXX', 'gracenote_id': '17927', 'logo_url': 'https://yt3.ggpht.com/nWUz5FbTXFWYF12_wutIBjPOL6U5cdidWZH7KWk56vMLD6V5le2iMgBacPMmLnbhrWO9NClQotDK=ns-nd'},
    'UC2vtKivdDk-kjg_qTzZTzUg': {'name': 'Comedy.TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/gTWVwuQz3nayWjL2CM0nQGRdW0nIQHit4nfWURS3Qpf6VAARxUKRI1qGYfmR9LuTV8wbZSu2Hrnl=ns-nd'},
    'UC2zFZSlhcDtpYBUr57eymPw': {'name': 'GREAT', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/mheXonCSypKTFYibm6YWf2R5bY8mjMYP9nOFurb2QIz6_Gk7RJK3yxB9PERzkQbKiD0Egb030N0M=ns-nd'},
    'UC33MnhgSnxj6gi6u78KFsBg': {'name': 'LiveNOW from FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/c-o-XEHtnLDK7f2AnPkleOTTctdNFTEmM-pY23q-U9Ao84xbMwExzcYFD-23YX104AMY0WsLRPik9w=ns-nd'},
    'UC3A3AJWkUb8xZnlmL6ayOOQ': {'name': 'HSN', 'gracenote_id': '62077', 'logo_url': 'https://yt3.ggpht.com/FW7x-Tbmh_GaFIzT62mNLHn9aJPIrcR3MFAH10J7qFTkpVpMxN1e0qLX5npl9FJus1I3m8tLmt_45Q=ns-nd'},
    'UC3KZEZtc4Iz9A_x6ms5KMOA': {'name': 'Discovery Channel', 'gracenote_id': '56905', 'logo_url': 'https://yt3.ggpht.com/FxpxjZamVdZDaJ4sJG4QJxSZbj0d5q16A_d6qUMX8nR2yZfxVcDZSPUHA0FwZsCxJFM-i0oHmIw=ns-nd'},
    'UC3UMF1UVLKV0-nHFIJRlVcA': {'name': 'STARZEncore East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YCZrisWnk00VKocyOG4ySvh6rdNzAqow4ofLPXpGlmracSJ-WJkDqYtpQwySvEde2M3PcT_D6DYP0Q=ns-nd'},
    'UC3fxb3kWIBzjqQqf7-FhNNQ': {'name': 'Zee Tamil', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/2UIvFREme830jLT3NNhR-z1ermJHaT1IctT9G2f3uwydPRIENcgEC8AXlBqiVIDpoHxgC3C1iTmNOQ=ns-nd'},
    'UC3g9rzlm1aasACEwPMRV-5A': {'name': 'CMT', 'gracenote_id': '10138', 'logo_url': 'https://yt3.ggpht.com/oYyMWfVnXZbha7kNuS-FjlCCT6h9LkLanVU8ecIIXl18kqYCDXH_AosP9MxV4zJzvvAhCFPywqk=ns-nd'},
    'UC3kLJGXBeCvwdJONa9lxdbg': {'name': 'Sports on HBO Max 6', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/IFE-cQe6lEqQ1ZPG2f4VitAz0yC6ZRCzjtSnoQva6Gu12UXZHnuqOacdTPyTDDxzrncNXC1CRyE=ns-nd'},
    'UC3rkSU0RQVyFYK-roH4jOnA': {'name': 'AMC Thrillers', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/eKaAnymSovMrFr4befb4azb5VT5gBgYDCUzmwAh3oOk5aKFwtlX77HBG-us1tvnEY4vba1-iI9c=ns-nd'},
    'UC3yf-Rw0bg-iT0q57dhHkhA': {'name': 'Showtime 2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/U6yw1HYddo42qASdtTvp-a5q2xKHV4jzB7TEjozb8EEYoQPYN4BxT6pgIcTKR7LisGm0a3fdWZem=ns-nd'},
    'UC4C0FjxHuZcNMG8X3IuRtjA': {'name': 'Huntstream', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/pigpyN9tPKDAbGdXtMa6ZIt4_-njH9k29DVCRWgTdXQOD3os6Vl_J6NA4G9DhnwFnBvvBL4h158=ns-nd'},
    'UC4S7frnF1aFbe_zcygsYe3A': {'name': 'SYFY', 'gracenote_id': '24533', 'logo_url': 'https://yt3.ggpht.com/xga3RCfcTxqhWHN3kf2EM-bhb5ia9FmtrgZBuJRqLTvljkG9qHtB8EFRRuWlYjtRp4T-B19yoRPi=ns-nd'},
    'UC4WzX4rjxyOavZENjRCHEaw': {'name': 'TLC', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/h3kuqfQK0gDWKXsNkvPjESsn2o1sFyOF5nZcYi1bi0g65zy8RCzBWExQgiJMU8wuRAF64Q0MV3e5hA=ns-nd'},
    'UC4p8zzrKOEseTaSqdV5rgSw': {'name': 'Sports on HBO Max 9', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/ApeCuNrkoVk_QUVFz7G-a7vyONFCE1zOIguAmhitXqmDu61luTm0IumtsPxvdujNwlM0hXCz4FCC=ns-nd'},
    'UC4w6UuRZn1xYOjVbofVeZ-Q': {'name': 'FOX Sports Plus 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/fnHjhZhu_eNp0QHHxRHxDiQejLUyIctC0QcVJMv_RqFVrIm5OryyuWJ7vZlLdZerdfjw3sgw0Jeh2A=ns-nd'},
    'UC4wjlJkClqpCIuim_Z-rWuA': {'name': 'NFL RedZone', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UC50J_pyw4NDuXoq7o7_m6Hw': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UC5x-DRotxmflDW1WBy3X4rw': {'name': 'Zee Kannada', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bzew-P23h5h_lLGmea_zMk1Lcuq4Zs-cNeDZ9XcTVEXnWJHBimQGHU4ibNjMklyStxW7oqq4m_ky=ns-nd'},
    'UC6e9zqzPIz3HgBfOEVkZX3A': {'name': 'The Nest', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/3qz2qwR9C6xzj9cjGOAl5cXipms3G_TqKTuO8ZNzxHefwEM6VwBfFVCWcg-55ASroUbOZ7XbnC4=ns-nd'},
    'UC76X1QPFUNE82GVtnLY5ujg': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UC7I_eOniDUFbPeESYDeL4DQ': {'name': 'Magnolia Network', 'gracenote_id': '18544', 'logo_url': 'https://yt3.ggpht.com/XuiwjscSB7cKkesrvlxaEzXsa8Do0DFZ4mIuwfYCVU538zplrYs8jExWpbQl43FpE7gKWSsWnQc=ns-nd'},
    'UC7Ir3yo2wlwLnV2KEtTMCoA': {'name': 'Comet TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/fGJVL4ZIjA5D45wR-h1W5i2WNKBanpF6sGcL9xlwwOIhtMThYTmoNdzqrf6TcjcMgHcUSskTAHob=ns-nd'},
    'UC7wZavgRQuumVn0KWgzBwEQ': {'name': 'ViX Premium Deportes 4', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/SA2MfjQVfQO96cA3cdRnI1-Y-zfGHRUZBesFBmlzuhVT-clDO3t9wzzZ0w_McN6YJ2gEw8b4QHsR=ns-nd'},
    'UC82DH9XlBvAiTPPcGTWldaw': {'name': 'STARZ Comedy', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/wOhdEL6jaX7lFANAclPe_U8xMJKap51nN55xdbjxeSNAol9gzE1VN1YRWPfKFM10j5gtx3JnSAC_=ns-nd'},
    'UC8cihSzdMLhd7Z1LpHubbTQ': {'name': 'MTV2', 'gracenote_id': '16361', 'logo_url': 'https://yt3.ggpht.com/zBOW5EtbGlhkCyUxvL2QLalmpBIUE0uZMnG1bjCbbORI9CjHiJbr2ICpz03CLgmW-fBj3Bp7hpE=ns-nd'},
    'UC8e3UXoeR6ph1_DqY8lgK2w': {'name': 'GMA News TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/H6kt2Wy7-USO8vN5Hex2rE-QtKXg0QFC01k03g_osgiHMAILmV3p2NXlKmvVVJQif5_x4osmbxox=ns-nd'},
    'UC8f_6NxAk-x_HqzrIHTem9w': {'name': 'FS1', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/thwdafqqkdn3hdrOvvsvngRlxEB2W8GOOgeGM5D6W_aoKprc37miEhKO5-q1lvNAY63XejLl8pRK=ns-nd'},
    'UC8j-HIVNIgtewBryQ4o2k8A': {'name': 'SundanceTV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Sy4BWJYsoHyyF3hWluLh0SH68znXN_st20qY-YCdxIKB3mKidIcvJDg_wVhsK2Mbub2jwIvfxUs=ns-nd'},
    'UC8lqGZa37U4OO610SMbyQJw': {'name': 'FOX SOUL', 'gracenote_id': '119212', 'logo_url': 'https://yt3.ggpht.com/qxj6P3aPiUzfasY3B4-CVJD20rMnqrjGN-IG_eFcxy8OQuQK9n2EpjmUqHuBq_SpRiIsuvEX2BaM=ns-nd'},
    'UC8sIulL4pj-n2FVQhK5P8SA': {'name': 'Zee Talkies', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/96koFPWUd5rnsHC97e7R-aVoSHGrARbPx-0H1ULSEAVVgmxvvF4j-W-FASuc8zcijIG6TuZJqQPyxQ=ns-nd'},
    'UC9oV5ZyHOEfA93G7y-vsnVA': {'name': 'Cozi', 'gracenote_id': '112981', 'logo_url': 'https://yt3.ggpht.com/1RxLu2HS16ThvzixyYuSfeWVzFjlPDFR5tEyicwhtQnedYqHb-_8ywE3UiZ6UAlokW8cX9iMeMwi1w=ns-nd'},
    'UC9yl4wui7bT8T6uWnyWiqwg': {'name': 'Sports on HBO Max 2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/zWbaJPsT3KrCcuSr45vnAfmibzjTSV7y1OssHDOHFSEmeV1qIeLmLMPXz4EPCwS8qy83t1t6Hudu=ns-nd'},
    'UCA6ODcHN12JiMJ-D8qOfW_w': {'name': 'Recipe.TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/2KNwsOQDj2AdAy0PAUJxqPgwX4wO6sx1izq2QZ81oTG711gT6Ys7viS4NUWvcEghEJhR03FQJxNjzA=ns-nd'},
    'UCAPNFjbcR-BgGS16FksVFEA': {'name': 'Zee Classic', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/NOCFY2DlNgAYgqGhigNCgpWw5XJEs81JZdGX5lqxkHl_0M7s8stCsIbb8vYslHnSesMbqOieLJ0=ns-nd'},
    'UCAeOGHZcZr1SY-CiLUZAP2Q': {'name': 'Sports on HBO Max 1', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/uBHMst0O19SWSzNCynieeVy6Az-0JkNqFNzjsJPC5jtoXB6nr3mVdyFTYq7BjyyWhUcodLFo0_jC=ns-nd'},
    'UCAma1G6Dh7Wx_28Tr0gUnUw': {'name': 'Sports on HBO Max 7', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/WuWrxmmfi3Oqlykge7RBPYoyU9UjLOHgMeopB9o6o6Ov5m5_dUF6I2QL48dldDPOTnue32qWMdBp=ns-nd'},
    'UCAuZjoXwexKxzlVK9zGNRNw': {'name': 'HBO Movies East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/iFHJjuy0lpBzwhGetJiLpiyWKUegkeV8OV_Lyx935fvEzU2zE7KuvoPClBSuxiGqhvURS1nSaeA1=ns-nd'},
    'UCB60DsHM5eD6FYUv_kw-7Ww': {'name': 'MGM+ East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Yn8txHE2dyvFHvoYwMovZZ31wfY6zcWRMfugBr6bDMtq8ldci2ZmI1HnwWhOxIPSRNAenFh1hO8=ns-nd'},
    'UCBPltzZR--hTXRTdYkJWu_g': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCBvMULs2YrbFjhPJQBKILhw': {'name': 'CNBC', 'gracenote_id': '10139', 'logo_url': 'https://yt3.ggpht.com/lTojN1Ks1DMUfdPs6tjHuf9UNorPCDT-x6akgz8dHrtazwxZVV24I8Tno-tSQBdDVwUaovGP1nQ=ns-nd'},
    'UCCYrcqAHdnJzkes2xe_X4Yw': {'name': 'Cartoon Network', 'gracenote_id': '60048', 'logo_url': 'https://yt3.ggpht.com/TkWrea2su2JsiOhD1SfxMA0BRw57uR5zLR3FBAPNukSanl8FnKP9qDvX7V1oFiSBaZ6d4kIj2Sw1=ns-nd'},
    'UCChSzsVWe1H3Kc2Fqn4atEQ': {'name': 'Golf Channel', 'gracenote_id': '61854', 'logo_url': 'https://yt3.ggpht.com/04MK8xlwm_vPr5tkUyGD39-Ecrmbfma2-NmrzLDKNqnVSX_HfNoxQ0iakOUHkoP_2VmQMx4aXpKd=ns-nd'},
    'UCCji-FUr16pJrPC0QGYX8NA': {'name': 'STARZEncore Classic', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/q7oznhXsKo3bjlJHO23X2EE0a2-eXgT89UL1QJou7kQooRZhJJJbif6X2fEdkO-EvPfmgPytaWiP_w=ns-nd'},
    'UCD2hzWipLHCVmwHux7oZbKw': {'name': 'Scripps News', 'gracenote_id': '96827', 'logo_url': 'https://yt3.ggpht.com/PSKD2tqpYtoiC6pY4SrJgTVlFAIM-pXW5DbefabN0BZOpilArjoNixMYkU4RoiP9nTE_rd3H_dVsjA=ns-nd'},
    'UCD9-oAYJYLAX5GAKCz_48SA': {'name': 'TBS', 'gracenote_id': '11867', 'logo_url': 'https://yt3.ggpht.com/FaPLn44fnF1Ntn8KkZtaEpVTyQBVxDJnRbbUOc5whGiO_z-U64b6pQ51rkejJa1GIGk2SEM1Uhrc=ns-nd'},
    'UCDRP-3aMlEQ6uoYm_RKeCOg': {'name': 'Animal Planet', 'gracenote_id': '57394', 'logo_url': 'https://yt3.ggpht.com/825pVVDg3Z7f_FOEjvwQxg4fhovfHyHVoh3nQgJsMH0iatOJuIuceLyVZIJyTAksax7wHymOq034=ns-nd'},
    'UCDRjb3xkbW6nNYeqjr2iZXg': {'name': 'ViX Premium Deportes 3', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/17lc5Ua76h9r-ka1TVbzdJbDwN44Pe1bGma-ujocIa3_gJgoniFGA9xeruv7V9wpWnZ3D_FN69waXA=ns-nd'},
    'UCDn_3JCrVk1CD8aBWvW-QQQ': {'name': 'TeenNick', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/08cgXeNdI2PSn0RRoEmpYJ2YYQCn42W_csBAbsGNgqT3WY7aZJcAjNCaQLi7vNwIOsP-IZqwDmU7OQ=ns-nd'},
    'UCEDGa3iCzYDSBCdC4iJU6xw': {'name': 'SHOxBET', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/pPNHBCLltCxAnadcE23lzu3mAft26y_zyedcbEgFmtBm_kkGGlH7BhUq3Qs5CLv5W1nZGymQXMWJ=ns-nd'},
    'UCERQ1rgFP5AWpoHQUSbP6cA': {'name': 'C-SPAN3', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/1wXcVVlwcDMMM8s1AmZP_elq7Wj1hxkH2OWKNdocmK_QUkFOokIcoqKv4LVHDAMix77Sxt3zXw0=ns-nd'},
    'UCE_hCgEokRm29XFFHusTKZw': {'name': 'Showtime Women', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/N7qYnkcjLaiPZxyaKuTtSzCleL0vpM-NL0LIciBiEG0am2mLnN0YwW_km8Z3ZxP7REQXC8upLCc=ns-nd'},
    'UCEfD9yuRsH9ZIqmnHHBJfqw': {'name': 'Showtime West', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/zPY2YMFdCRXcQJUmpN4KiE_U8odl3BDcmjxgtfxg-TsClH9WKa_fXYOW0jGCaSSvh068A3aqpc0W=ns-nd'},
    'UCEo26zV6dzpkFvEd1408olw': {'name': 'Nickelodeon', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/jaf8xpF-o2WIz66xhl_QPonYAQcNzKlxv7zv_AEJdMGDlIavqUxl21Xi18Fe1Fkoa2svZCqpgxUu=ns-nd'},
    'UCEyNozLxr4I7rZimVumfVHg': {'name': 'STARZEncore West', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/iHILp3yGTiID1qtzSTncZ5oPeIrL5bMLzHrNTGy8U77smp9xJ8zb-QvYffgqT7lYpW4TiXhEil5Wyg=ns-nd'},
    'UCFLhrU066jSv90ogeYcvq2w': {'name': 'FanDuel TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/nuikJEoAezSXUCGH9WfiQPsRprJlI4JFUkr5oQ0j-57Yl6rx4TroadicOZzHJAiCLrDrgI34Ci2I=ns-nd'},
    'UCFSR1oVlgsOSiQe4dPnBOfw': {'name': 'BET Her', 'gracenote_id': '97358', 'logo_url': 'https://yt3.ggpht.com/gNRhVWugqOcsOibfHR0bHmpHAP1SqwOP1gTVsKw2izFaub4ifwvcRuzoKcaTqrDxSUWUdr2Oyy80=ns-nd'},
    'UCFb9FWb-U9DKNwfTeCnamTw': {'name': 'Cinemax East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/AdVExfHMrQ98yyrH7xPPJ3J9KuN1YetUrwj92KATf8JrnjCAqwzIP-LKVTvcG0-5GVKzKVf4hPqUOw=ns-nd'},
    'UCFehJp_rl3lm8-8OHoqO3gw': {'name': 'Smithsonian Channel', 'gracenote_id': '65799', 'logo_url': 'https://yt3.ggpht.com/2vHqC0KRb671Fe6iKRHnVJh5GMPDhY4Sk7_K0i1OEDP4N45gsTI9qFw-zuwn_Slara3XRp9Knok=ns-nd'},
    'UCFsj5mvKOU6gEkbaptDHc3w': {'name': 'FS2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/pld7BSaY2gkE9VhuJTdy6_QwID4VtxmtBYlueIutZa27HOJvF29S7yZQp0odReLQUkABMq6apwak=ns-nd'},
    'UCGbypKg9TspCXLL3yYzz31g': {'name': 'Disney XD', 'gracenote_id': '60006', 'logo_url': 'https://yt3.ggpht.com/apfRUzeu0vC9219cc3_s_h-6j-COZAAytfQtrhKbzifd3B5Pe_YLvUza2aaHsIGmUPPX5p42R0c=ns-nd'},
    'UCHS5Yi6DGuW8ljpK8fd8Vsw': {'name': 'Stadium', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qSMrHacRdzFcxucvN7yQ-nwqXWgY568YsVfGXKarW7kE8c8CQv9iibaLMgm_z7YY_WlxPGEIT1iE=ns-nd'},
    'UCHa70oFCrg1M4sFJbhV5L7g': {'name': 'The Walking Dead Universe', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YWT6c9bvjiEpghlCmpaQtH2e831Cqloch0bPS-7A8lxKBionZMboP6HVHmxGKtEhqwN5b8-Mt9Q=ns-nd'},
    'UCI-EfxFyywB_IrXCyUgLYow': {'name': 'Dabl', 'gracenote_id': '112976', 'logo_url': 'https://yt3.ggpht.com/G645adPa_q_pQytOJm624obxBT9eI8nPPPEVVHs2u_7n3UmgGkEXB1fXqeUUfT39HNzFbDaymh0cCw=ns-nd'},
    'UCIMG7wOFkuGnuo5ZwwU08-A': {'name': 'ScreenPix', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/CuD6JC32WnNfiy1lRbQeFkewWzBhNB9Sibu7fvjhUv91q2TNlIZmdwNkA4fPKIUv4TlUlRjh0FAx=ns-nd'},
    'UCIVWabtxQrh8o2lzEu4EvBw': {'name': 'MTV Classic', 'gracenote_id': '59054', 'logo_url': 'https://yt3.ggpht.com/lkyn4KFW1hcX8ORoNmX7huRZjer6CkAenXyEKuVLTz3sIgOAQozzLQOQsF7uA5piJ-c_0xhHNueS=ns-nd'},
    'UCIZnLsx7JgR0b47tsmO9j1g': {'name': 'Paramount', 'gracenote_id': '59186', 'logo_url': 'https://yt3.ggpht.com/iM9funJ81bqC5R020AWbSS8lW10oIlR__9VyHhxbZVF7nqPD5DYcl5GqLjYOwTuO_XXVhflpO2ef=ns-nd'},
    'UCIfeUa31arBgYR1GuDzMR6w': {'name': 'VH1', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/oa9B0WO1FJz_sQQmu6tDcbAun1gvgKb-_0_SyX5WISKA8M8lrm8wEJ7xMwGpurVbszO5JWe8jmgN=ns-nd'},
    'UCIfkBELe9JsrKAfu97rcQYw': {'name': 'Local Now', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qOgL6R9uwmePsdnrBTOeNk6Y8cgiMQWgLprrtha23OXAFw9_toB1voMeF7HqBh1cV0x0Ym4j7Vk=ns-nd'},
    'UCJ1bMGjLxUVz_uQL-3UyvlA': {'name': 'Start TV', 'gracenote_id': '109758', 'logo_url': 'https://yt3.ggpht.com/ml6B4HgaHwZsWENWKLB71HEEbGKAy-GTQI9KjTqd17gM8APP75Sm50wPHVZFrURUFn0_qUeWBAvV=ns-nd'},
    'UCJ5alUuWAQAZMwDJf5XMUvg': {'name': 'Willow', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YJLX9Ocxp6ACKow99KznWfF2pW0ZNC8nXf_Et4iLnsA12YCaJBaMl6WMjKe9y-WtQcOwZbE-r0T3=ns-nd'},
    'UCJCcn5FyhJeu718vX-7jnJQ': {'name': 'CBS Sports Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/khhpq9spRVYVJpYxk0fQi6rAGIvuqvQ2ypnDbpUxjiPJIVSFq9hwE1RPp6fTIg-68lr3buj-1ajV=ns-nd'},
    'UCJDNzpdVXySLwZPI5GwhTNQ': {'name': 'Discovery Turbo', 'gracenote_id': '111101', 'logo_url': 'https://yt3.ggpht.com/KAIRnmjWcqSxsBq8S_LSEt9Toghboces0r89XaiL0x8oDOA1KS2j1Fw_YR9-rFmdGpXLe3VL4WA7Qg=ns-nd'},
    'UCK9Q4fLemL37Au_y0UgNM4A': {'name': 'World at War', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/urlj5qg3cSXVRkUE7kS_f43VbRdg1fOkByCSB8Oh6RfRPN4JNyk2igsyVNNh6VZDEN3oCa9LmIN35Q=ns-nd'},
    'UCKFEpiIpLF7bf_Di_uuBhwQ': {'name': 'ViX Premium Deportes 5', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/IitLRJ8mnmv93jIy8jkCTWbcz_h4FAtnQ_ltbov0J87nRdIV5-6dNuEb7zBw0_tPk8ZSIVIJV5w=ns-nd'},
    'UCKFToknzO8tSIK6YsCt9-QA': {'name': 'Cars.TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/dNtBIXVhfCp-B9U3zTP0I4e-t5XbSOLHuz0osMU3jzAmAAE1aCJOxl21-TCcnacRMTddho8r7eQ=ns-nd'},
    'UCKJIDt5EnyjGcuvluFCWOJQ': {'name': 'Tennis Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/C66bMcmZ6lNNu3J4XgJthRsJHl6MAf-ITmZ6ikPWkJzA9Kp1eyEHdPSnMZ7T_Y3bdFSI4JaO0njA=ns-nd'},
    'UCKmS_J820Nlwo9RvcOvdoSA': {'name': 'Cinemax Latino', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/q77FwzvtCcROHIDZU-Wfu1veiLhHjWYrhw0Lbw2ng-JwfCNgRk3lExTO9j4FJJGEuTk7cFt5ruingg=ns-nd'},
    'UCKowx-8h-zSLiVshzKgcsRw': {'name': 'Galavision', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/_dRlhnF-coxs42cVDS85uuiTwLnPIOqu9qmESojAPdpQxhDqzTD29WmLaq_hNQSvx0XTE4HjHJM=ns-nd'},
    'UCL56IuYSxfOp1I__FBFtM7w': {'name': 'UniMas', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/vFbEZTByUkPXp3lk8DBDnKCl8ufmhX3LixsJhzs4RtO76yZDYoj-VpuIYTqYkjVi4HAeu8lCsGM=ns-nd'},
    'UCLwYlIIdXLEw-DR8R79NmHQ': {'name': 'FOX 28', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qr9I-WMPoZqn1PsAzy-JouQiyB3mqCsnk-aRWd5dcOwr-aMzytODm9mVgFf0mtDLhSUsEzADSOA=ns-nd'},
    'UCLzVssMTD4F7XUR1sh3JZMA': {'name': 'FOX Business', 'gracenote_id': '58718', 'logo_url': 'https://yt3.ggpht.com/fJuwb6zxeBfBeW3wy_mFjHnH87tINxeUcE7IXVPtCGOWboH3p-x16sCdGFvaMpCMzPJZdvSRyAWO=ns-nd'},
    'UCML7-74jlnw815NMxZWSGpg': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCMQqhZBTHj7XawJbHi_76sQ': {'name': 'Sports on HBO Max 8', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/DpQXu3HT5PpuPoxFclQbpmZgLsE54Mo00uztX-OcA6-b8Hj8NEw4caAWKYvFzxng5g_unZ6IZfs=ns-nd'},
    'UCM_xfbqClkDqG1NvTbsLFqg': {'name': 'NFL RedZone', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/l--YvhxwxVRjqfQguHPyinTmDI-4hM2uA_xAdWJKt2a24yqR4jY-8zQUjc0Ibjhoc7yeNXJcG3Zx=ns-nd'},
    'UCMjbio9a1V40GrsZBh1R1Rw': {'name': 'Sports on HBO Max 3', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/W9JYfuzH9kimDPTvTmDa1d4aJSQueS0_i0hjmVw8Z5oYXoCG1KXIUqWkh0TzL3V_7iq7tTEsZ4Pt=ns-nd'},
    'UCMp8D-tBjj4ZCsWyyOkIX_A': {'name': 'GMA Pinoy TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Nyy6X-pkY0pwv8cY0eFk4-OApg5WowSRDKcv3zZnDRzOz_ANoYmHQvdd53CF7LEazeO7SquivJQo=ns-nd'},
    'UCMrLeKNQLIcj2l1k4yqkILQ': {'name': 'TNT', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/ResZjOCfPklZIyzwjCHmSA0vUqYewchJFVb3WChU1EO1cvqei7ZBr-FQEKTKkGdeR9L7o4jjbnKE=ns-nd'},
    'UCNFMyKDGUWEWD-xO3bQT3Hg': {'name': 'Bounce', 'gracenote_id': '55314', 'logo_url': 'https://yt3.ggpht.com/7Px-5xymWKjwneKv5tu7pgXSmct3CSQX8FFGe3bMAX-bAqXHIT8LrPxlOlR_cWqDo8r2AQHVvJ86CQ=ns-nd'},
    'UCNd6UJFGnUUjllgfXMz_Y7A': {'name': 'TUDN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/0JRZFTGhQMoAVJ0Jh7TZgATfTVFKySEWsIFxzzczft3KadPte6yPOj4l7d6BzED8pW-ONsOi8lp_=ns-nd'},
    'UCNfUuHSWBcaZ_qz1v-E2Jlg': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCNp8TLrPZ6VB52E6tjqgu7w': {'name': 'NFL Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/IuY4w91B-kITPAhtocGjjbapgxe5Jop5MlU8b4uUT3444lBUErmxdqJL5H4cWd86TEVP-absl20alw=ns-nd'},
    'UCNxCYxwUbCRBDzA0RB0bSDw': {'name': 'Oxygen True Crime', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/LLlFscYYLrlyCftt7yXp_tpbh3GCZ3_44o8uCrysv7jVaHzi0wtWuFXvEcgW-UKEXeWfwm-fTqk=ns-nd'},
    'UCO0DprpzkgRs0zpSGxMI6bw': {'name': 'Travel Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bsZ0GLJy2Iauk6RTXfo4a2PLoyaZn-B3uP3XZ-JAf5lZ_uFbaTJxRAtDAO0PbSC7yYcHLwXQpSCW=ns-nd'},
    'UCOBaG_GW4MyOXCZYBr0jxlQ': {'name': 'The Walking Dead Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/E1lbqvlMetRxjnTyrHADbgUMJVwUtkGMGqomOj4DP1ewwkyUb0bvhuq4dC4NlVTtWmJISQ-PTkU=ns-nd'},
    'UCOU8W2EKGsLD10F2Vujo40w': {'name': 'STARZ inBlack', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/T83_pY23eUaBuwWnYGVqTgckn6lDiTqXOpmStqT4D2tsYICnMr48m7H_6oIwcJwvf3YCsREMERA=ns-nd'},
    'UCOjTLqTl1gVybbm5iOT9o9g': {'name': 'FOX Weather', 'gracenote_id': '123194', 'logo_url': 'https://yt3.ggpht.com/WMenEEPDRBQb2HvF5IFLoHoxAxMwzLv1dQ5GoPsDkBwK-iQy96Z1OWS18NyFgGo7fywFffvVLaZMNA=ns-nd'},
    'UCOtYFLYvZEiaZw4YpSjOndw': {'name': 'ESPN2', 'gracenote_id': '12444', 'logo_url': 'https://yt3.ggpht.com/hwBIH6ajs0KRlc5PrxjfVCqh62ssKztUscHUFcEOynOUHUmndT40W-vyxoXNYX-WLSQb9Ek4z987Cg=ns-nd'},
    'UCOxq84gl5aYumPcJv-nYWxQ': {'name': 'Zee Anmol', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/ecHQ0ib2LLbNuC0GD0XOARhn3iEdraSBaG6G-fjbxorYG6b6zQ1qj76Xh8x3F4sWAlczRuYVKzU=ns-nd'},
    'UCP52ybr2d5kw2FwHZiKtR8g': {'name': 'PlayersTV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/NUnM_Stqzn5sD55fOqzjvRfOTe8SohnDgMzHBTkh1e58QGXRIe8YafrTibSu45yJfYSSQICgx2_n=ns-nd'},
    'UCP7l2eYJokMkMQVwPs8WB_A': {'name': 'FX', 'gracenote_id': '14321', 'logo_url': 'https://yt3.ggpht.com/JrOxmfn_qOkPx-95hRfK7dxDAFdz8XyoMkZSpv-2qR0IxzZKFajjz-_n11WuVlIdUGqGuoqXAOVb=ns-nd'},
    'UCPIvHJrgKn3AWMhkMA8EoPQ': {'name': 'HLN', 'gracenote_id': '64549', 'logo_url': 'https://yt3.ggpht.com/oBMOIptDWpA4N1wjMIfNY5t3iKrnBEKJnQDZ2DairRhLLj-YGe4JjjqLDXCgHTHD9k_Lk505kNc=ns-nd'},
    'UCPJeD1hQS_-h9u3xCxbRg6Q': {'name': 'Zee Cinemalu', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Um0u0juzRH09L1FdUmcclgxcpdguwsAm9xDWyVLqbs2MY5fo83xXNAOpMUhFrdRTO5XmlrLMy1Gi=ns-nd'},
    'UCPMoZoTUinLM-MngPHWijUw': {'name': 'MS NOW', 'gracenote_id': '16300', 'logo_url': 'https://yt3.ggpht.com/EmL2SojZWhcSnGOXmDzCf13QsYeHKhwkUiWZp5pGeeUU3E8NsJbz2Dqu6liyuDRcLvccF-Mz-gtA=ns-nd'},
    'UCQ7xrr_hilON4yXPhwHaheg': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCQBq9yc19hqHFxMhFLDcXCA': {'name': 'MTV', 'gracenote_id': '10986', 'logo_url': 'https://yt3.ggpht.com/5gWJxRI0WOms90P7H5OIIXZuByP8O2ompA9UIrA8zKyiAVxMumwZAq3dZqx5p4yplC6jU2GlO9eO5Q=ns-nd'},
    'UCQF1zxktny4KOH2PgsSPZ-A': {'name': 'Zee Cinema', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/OcxMnQfwgYf-drj2AGxFsowmJkod_sRHBjiypgtP5IbTz-UAYZg7kL80_FbVxtkICBC02JU8PGq9=ns-nd'},
    'UCQRRBYgUQJ_EhwqS_dAy33w': {'name': 'Pickleball TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YnIJEkVyx43-jkdkg0zjv0m0i2PgON6WUWH7Czc6M_WEmmJPA6CQ49AGSaN9UiAf6p2TeQ5vf_L-=ns-nd'},
    'UCQvoqeSqx_XwboZCPI4YVxQ': {'name': 'Zee TV USA', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/sxt1jM-P-qXQyCt9b6c6DBWuNzPVzzsMVq8OvqxuArFSM1DRVYmdWnKfsfHUi_w4NgtOY8RJSFs=ns-nd'},
    'UCR9hPhAxagm_0FdbX_1UCoQ': {'name': 'HBO Comedy East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/vZ7-JSUJsZr6yTw7K_Hwmg5835hnq3tldnsA3A84Z32GKcfdsU1hKKwCjqUGYDvRvyc46JjEVe_o=ns-nd'},
    'UCRBTHNanZNbX0xccvuh2aXA': {'name': 'UNIHD', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/94c3TWaYRJu5e6t5uQTQuK_e5RGVmBbAJ55k27EnBcZBd3rLxAXq7DDBHiUkQD9ky09OpKiFBCfB=ns-nd'},
    'UCRN_m3MAWsiQ8lAqsm1XYbg': {'name': 'Sports on HBO Max 10', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/nsDS_1nCCGpzpWXKVCC9EoN-NCm7shZsRdgU7_mau5Nkh5xnqOnqpdnHKblbZT9lru8XxZDeESYFDg=ns-nd'},
    'UCRQcfaq4UbB7EisWRXD-9xA': {'name': 'Nat Geo', 'gracenote_id': '143571', 'logo_url': 'https://yt3.ggpht.com/bCJ_n4hJMJmMufe8N-ePSpKwIwDpfNwPfzi6vMnZs80hbJVKPVz9DnZ00OgsYZf-L8tnDZiRvE-l=ns-nd'},
    'UCRUaN9crBc_JInWlxOVuymA': {'name': 'STARZ Edge', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/5CHkjNNj56xzebvYsb-2IjqPN8Cy-YDMjbBmkUc0GKIwtVadZ9ep7idRdQbTflvPhFt9eGdU0iHt=ns-nd'},
    'UCRXEMW5x4v3dEto7JCkPAvg': {'name': 'AMC+', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/S9kIFiN2U_hAJO3iMkhaQ_pqwPA2rMpjHUm6zNCgZecPaQ5VIR_GecNFFFSMkPdRhhXqkEV4Co4=ns-nd'},
    'UCRXsF67RpQTpfgrHdCKoOSA': {'name': 'WE tv', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YblEDD2M60WqeyctQWV-hi8KCQedoyrPviIts_CjrMrIUccgWDBlaTVTL2wRm409wpKNkiqrYnCY=ns-nd'},
    'UCRiIubRNtAUghYKB0_KFtGQ': {'name': 'Court TV', 'gracenote_id': '111043', 'logo_url': 'https://yt3.ggpht.com/62IBTj8SVZUFADp3788iHumywufJ0TmTMjkvZDwq7-gDOuvVIJ_i2bsGT-huLSsm2UBKcenV-Fge=ns-nd'},
    'UCRmDpn3FghrsHZ567Z4qUHA': {'name': 'Fight Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/k-ojYZENV5hoxOYw8NJ1MftSIAvc6M9DvMKzrVhAsaGbb5VECMtY5r2W1vGy2uW3dT4ILLbx1gdBmA=ns-nd'},
    'UCRo4snHVh9VOJWdNzVgyljg': {'name': 'ViX Premium Deportes 2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/4QYs4slYmD7kRRbNXL8Yo7l1656aI5Mlg3vKLeHEPHuRP_kbk6Fegf69Ov2QBxiyjRSKC7Fw25g=ns-nd'},
    'UCRpa59GI1QwGkXq2V0kSCVQ': {'name': 'Nicktoons', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/-hctZObjqhbfW_YNdhEI0VzWZEwH1YcqA-1lAeJ5AUXEOpiQKl9EIWH-YQw8W5VyzgXukIpANXkQvQ=ns-nd'},
    'UCRsTIFJhpNwFPS2-Dqs6cnA': {'name': 'FanDuel Racing', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/XDZpK_oXWziJEsKrQVCJZmCtdu9dc9ZM1AijDM014nXBlD1F3Oy5yPa8S6Bso0GyUButJWj-4n0N=ns-nd'},
    'UCRwOhBPh7Md88TNvxvO48vA': {'name': 'Showtime Showcase', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/h7d7UPTQyQZC7McIERu6bxqRvHlJu582BSR9QgQ0o990QqGPnCYCwRdSxAKeDWszWYMUI6-qvez-0A=ns-nd'},
    'UCSRDwPEZ5tkrRx_oXYX4o_Q': {'name': 'NBC News NOW', 'gracenote_id': '114174', 'logo_url': 'https://yt3.ggpht.com/DQtI1OSl9qvisqQY7Xtk8U3QccpLluG5pwzfDEC65QC7gI2vFyjJFa-gh4NuuVZvvArdOvNCx2cXpg=ns-nd'},
    'UCStfhR2V58QkCCyq_8dlk6g': {'name': 'CNN', 'gracenote_id': '10142', 'logo_url': 'https://yt3.ggpht.com/RZ5eDBOHowGukv9TZ8XAkCjzQ7Ihbw2L3Z1bJHjUbhnwVHFHS-661QK_qKHe-lE_inBQUAVmPY8K=ns-nd'},
    'UCSw1UZ2F4Ee95QbCRLtY6DA': {'name': 'beIN SPORTS XTRA', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/YCSELLLWlCg3E3BfW2YThtP8hi8GAsy3Ip6y04uF8f6eW-5xqA84DkCNzpN9D6h08-kQURkWiGoeJw=ns-nd'},
    'UCTCh2ShRKVv--O3_bSOueSw': {'name': 'ViX Premium Deportes 7', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/gfxhq2DO5Yer9y98D3-eovpgjm2ChJ-PXKD7RPVcKYBM3xd4WnbFCfpEIJrlWXRDsWQYl6gAUgk=ns-nd'},
    'UCTUJNBSUDlkibFgEsYMJdQQ': {'name': 'Here TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/a62Tl_z5pSiB-h6L1l7a8IWv0IhwwmFHRNV9veOzmEJY9PxXyNJob7mg7-f-oRXEXDZ8IDL9f2jI=ns-nd'},
    'UCTe797AzsaqogeVoXG-gnJw': {'name': 'ViX Premium Deportes 1', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/9hD28mABcOf98aoKwvCsYpZQztxbvskYlOgqYQVm49Aoj-3rrx4zsJARB2v7Z4d9rWuAX7E7voM7CA=ns-nd'},
    'UCTzz7p-gzo-fdwo1LQ5KYNA': {'name': 'ViX Premium Deportes 8', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Xu5rrNbyM9u4NoQXEjD53FhSqRilc71QBVsmT32AyDki5baZrt615evZTAfxPlrubP8Han5_hcHf=ns-nd'},
    'UCU2BV1ytjSc10dXggP3zCLA': {'name': 'WNBA on ION', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/vuXZY8Oy9Y-QzOMDst_OWedcI7N3FICZaH_tapTnL4Nbia0n2FlfNuQuzJs8TjY6Rd5yBeYiW7Y=ns-nd'},
    'UCUKPIBgCHBAi7dbVeZv-GxA': {'name': 'Cinemax Action East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/cu3H6_pf5IR_GgvYWTj4XSb8vyHr2Zs1wZQJ0YZh059bWy55JhD_KWyodTKfxo9ty42UWXRqiV8g=ns-nd'},
    'UCULL3PM8sg7l7yAEocqkZ6w': {'name': 'Cheddar', 'gracenote_id': '107241', 'logo_url': 'https://yt3.ggpht.com/o8iB9Ig7cshWpp_rvAHd4FOC9WoDDv7nT3M3UHkR088aTgbqcux87OAv7kJGacqg_T0dNCH8CpfA=ns-nd'},
    'UCUNWnw2MB07Bfc3BcevmZVA': {'name': 'NBCSN Extra', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qBOPs1i4qnh1ozfpYZPtP3EQSsFWlHt-2Jy0HGIbgQz3EbxWFD2DvrQRlIulRR_jZzlaictw5Zb1=ns-nd'},
    'UCV2pXl1pkayqpxL8KWY6zfg': {'name': 'All Reality We TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/xS3jjQ-1gAx1wpKiONGlAe9fU4Bpprt-syHGaTIG4h5lpQ3qpcx1_kTbsBaX1PxwTizXwiWIvDMD=ns-nd'},
    'UCV6aAjNXFAq4sM8NHO35wKA': {'name': 'STARZEncore Español', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/ISPQnYH3N66GgQJvLNQcqeYNLs4eThnv7jm8RRwNOZU8XC7J5slxhL05kiYbl9FZP8KlM0pSIbRT=ns-nd'},
    'UCVN4FmqZlqjiNT9uR48Ll1Q': {'name': 'Disney Channel', 'gracenote_id': '59684', 'logo_url': 'https://yt3.ggpht.com/yNsmgQiL8l7PPsthDqPY9BR1fcHXBBKarWGe92xuDgzAoxIyz3Jx4aaTdq_xMCp6pbqfZq43oqKD3Q=ns-nd'},
    'UCVQ9StUJS6wzebqN7KRgxkQ': {'name': 'MGM+ Drive-In', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/umVrzYeNsqcfAfnW4kZVvuQzoFR3ccWddjpfDjJvrCO29GPt0uDDP2aYkeQsItD0t7J9DwsqtTs=ns-nd'},
    'UCW4D25LMLsT08eFOWu9napA': {'name': 'NBC 4', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/2wNOI9Sh1iOi8ODnyHISazPvhFeosQB89VS2dPeplPDz9qlzQ3HBcJe7jUzNsyQbTIRz4SwZ224=ns-nd'},
    'UCW7W_WAogi3qWDbO9PqOmZQ': {'name': 'ESPN', 'gracenote_id': '10179', 'logo_url': 'https://yt3.ggpht.com/zQEsGs-Pl8rGEMxEAKrzuvnKYqIK_eG2Fw8As28gNgK7xOBlbtgFdwAHe9uGTimyCJd9_Q1nNfP35w=ns-nd'},
    'UCWBR8qGJOBAe5PJXOeMLB0w': {'name': 'T2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/VsqEMzlXBqU3QLXupVpsZIdBHRJiMVX7ychywlYm8MA5Ys71lI67nzf3AOr49VdtZSWoGCSJD3TZ=ns-nd'},
    'UCWkkY4JJBkGZtFae6kkUbSw': {'name': 'Turner Classic Movies', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/7_L_OkCrQdsknzNvPVlnL4yKTZgz33wjlYqFSLg8F1QiaPyUtYxaNbie2quyTvgdh1ZVP_ExXarvdg=ns-nd'},
    'UCWmVVvAgjJQzvI29zWD4V-Q': {'name': 'Pop', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/mNwcUTyHdSx9VFtoC1F5zD5EO9rWr4OD6BhCI0CwV0aBk0-Rzz8Nijmc34jzhsFaxgbRfJ62ti1vTQ=ns-nd'},
    'UCWqfpIJ9iXBr9io0yQsOeVA': {'name': 'SportsGrid', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/N7psPy8EVDGNk7ZZpXfrX7TaoFrQHrc_a9ks4ZN4aEmUSPAF3lCiKfEOSQ0oaDNLXc4gWBhrhVo5Ow=ns-nd'},
    'UCX6uEhY1FuU3yz7if36XlYA': {'name': 'Showtime Next', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/7rt5YdCppuTR-I11NF-hNKGmmIg0qNTsXO539GOA1koxNeQH3Di9kVT2w8c1QmbgkS9Zs9Mk0oGvNQ=ns-nd'},
    'UCXWzlfEGSllnpEnX4Id4jQQ': {'name': 'The CW Columbus', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/_dlw1nN6oAOFezmIsq4VB9y3S1iVNDXvwbXx9Fh1aXVdtJQV53S1e_P55QYTSM-116BswJteQuo=ns-nd'},
    'UCXgV84lI09EqItQYLxCrZ0w': {'name': 'Portlandia', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/M4CCi7Wxuz1g1Mm5MjjhdW6tzTLnR2WVY4g_iUP3ueVAPaaPxFqlVDkqzcDy03xThKD24QNyyE8=ns-nd'},
    'UCXhUSZzg6DgV4BQxjU5SEvA': {'name': 'WOSU', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/wfl2obryKLneTvKo9qpoMhNUU6eWNqf8dsyIQZc5syjs8pbI3OA4f9w7hwtrMz3NWE50WzQf77rrBg=ns-nd'},
    'UCYGMX0Qgf5TLKeLGPNRUhcA': {'name': 'WOSU', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/U8sssZvboF4D-Xri2OM-r5Hn3J7qoCYWLNMvMXfSXCSc0U1n63LueG_QiF1z9uf8jte3epxfkipZ=ns-nd'},
    'UCYJX5y3lS9rvsw75gQjSvGw': {'name': 'Bloomberg Originals', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/yNkwz5AsosZFfn0uaQ6p6DaaYGaTLTPdfHwVuQm1Bnmr846xplYa4EqcYxL-KCWZ4PVbgP4ot_wvZw=ns-nd'},
    'UCYLG3atJfxvRoVjsH_4zQqg': {'name': 'theGRIO', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/v6YujM66kTaY5MFgiVgUC9OR5rxWb2FtTp6234UlNdDj-WG1O1Q-Sw6xa9ohCs0UeAnj4e8gFsAB=ns-nd'},
    'UCYMkTuztF5_bV8nTUxzKzwg': {'name': 'MLF-Bass Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/fVC7kYZA9nzmn2nL2Fg-IhwhslwWOkY4w3eQPD0jZTEDR-WR7imwvpdneGqqnO-tkAAKLo-MumWlKg=ns-nd'},
    'UCYcks1y0PKWWkhQBMkf5uMA': {'name': 'STARZEncore Action', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/yV4NlqzK2jmHetRgZ8V7QhRe95YCYVD8o1deIf772G6urDRmFZVaeWaA6JxxW4_77UoEJurnpJs=ns-nd'},
    'UCYdjAW3r89cZr6m-nFGXkjg': {'name': 'PokerGO+', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qYXzcJD2Ef1QmCW6aQhWz-DR4Lv1A6YaOPRyUuB5SvySGB98z-VrAH6NdxJ9yDfV4VJPONZ71qOX=ns-nd'},
    'UCZaTc6ohEtA1uNyWD_5-O8Q': {'name': 'ID', 'gracenote_id': '16615', 'logo_url': 'https://yt3.ggpht.com/0em-Ox1g31jyexO13J-B_sfcVoV-dO_b2k3hhobbliMc-JaaoBoZC6TOInab9GQRv02-8jy1-zU=ns-nd'},
    'UCZwxosConLtVUgnydqL08dQ': {'name': 'HBO East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/VfHSZUHZP5bSdxm69X48vl2JrjsXHnDD38aIaPd0wTYiB4fiCi6RKRJdTH6gvLNHYB7oP_bWzabC3g=ns-nd'},
    'UC_bDR0QfFCkYB8RCtKHlorw': {'name': 'Zee Bollywood', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/dE9U3c4mwrgEeqKV-u_PPwhhg7O5jbLlZt9iutyk5ToRsOiEaTX1f8uTGdRzm5yxrDGqu-x9X9c=ns-nd'},
    'UC_iYDoUH42IulhrKQnUdLgw': {'name': 'Localish', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/jpuxw9C5iMA9knq3jla5k8GBepsxaGe0kaBq3em_NgCfNFUYG8dhY220VFSGPZj1DtKb4wnT_SzOMQ=ns-nd'},
    'UCaPEm-6YWqZTubSGqZaL8lg': {'name': 'BTN Overflow 1', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/8xmuD-O-olQmVS7-kl3I_3dVczzn-SCajPur2hGjiugTidRv5y7B7uOx5YWorXa6EIDxemubcRKC=ns-nd'},
    'UCaPfU4OoKvm-bRrsay4T4qw': {'name': 'STARZEncore Westerns', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/PXDGb1Sza_EZaP1lFmuBiNQxctsPJ1LSOAB1h3YAe40P8mMwB-Lxm_8ktVUjoiOMvkJSTSmOmOBI=ns-nd'},
    'UCaSrCb0caRtTt6uNLp6L68A': {'name': 'Zee Bangla', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/GX0drfmswSn8B4hSQiMqHtpi8yyEoYxraWbHPwcuuLCGYu14PVdrqYLnqRGmBo8_HHIRpXVvH1BHJA=ns-nd'},
    'UCaY_dmTBpOG9kWfXq-VLgtQ': {'name': 'CHARGE!', 'gracenote_id': '91578', 'logo_url': 'https://yt3.ggpht.com/1hXdVF-Pa1k2EdIqYIShk-BRkj1usItl7Mf2n4IRF3s-hgCRBiYkUoySdR9ezOEdATu5_DtRtFz6=ns-nd'},
    'UCawPT_7asTNbNwt-0s8P8lw': {'name': 'Outside', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/hzSeIxnF1fo_scf1F2H-0KOuiHa-ywHZ_aqqoE54sav6BL85c5dhKMFBCtlcBJVHyQb3PyM5AaLs=ns-nd'},
    'UCazXZSdH_UXDGvmSrtdfxuA': {'name': 'STARZ Kids & Family', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/QVLqfGCGaGo_hWDfnAIuwAxYmpDvI_Uefw8XcoFAii4wR1X69lH2HxcTJHKuMwUxfecvntPI8MgZ=ns-nd'},
    'UCb8Prbrv7Xghfdoc-jlejug': {'name': 'One America News', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/wEMK4PyF7zRyriSSSuRdsIvkiXZne2N7FUNxMRAQpvm6cHL5jYqAn7TC0inXpnu8vL8n96h8xCVbdw=ns-nd'},
    'UCbE0fBz-3rt6UW0mBvoAD7g': {'name': 'STARZ Cinema', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/qgiOD6nbuzr2y41XgPhdmzFlf7GMGHJhOqIVJh4qeUOxfH7UhNECiZQciKDIo1k4dQ0zxTcO_DY=ns-nd'},
    'UCbFaRGY8Bw3eLldVIFPDYhw': {'name': 'Bravo', 'gracenote_id': '10057', 'logo_url': 'https://yt3.ggpht.com/4q7Q9YOnRQEBNl3SXmOAiOFmqB84XtR6JGNDfVr0m1Bp1l1w6Xv82ToAnP-5KeCYEgRsd7PJkQ4sIQ=ns-nd'},
    'UCbjVsshw_-h8ZRQBYAacA2Q': {'name': 'AMC', 'gracenote_id': '10021', 'logo_url': 'https://yt3.ggpht.com/o-vK-nulLgoR9Ex4CoA6hfdQa0jZIlOot1TwptDpy6UE1p11wBhDwLukqWb7qvfAdIp8CFlaHygv=ns-nd'},
    'UCbji8ZLXWwsBOrw6VQB7IgQ': {'name': 'ION', 'gracenote_id': '18633', 'logo_url': 'https://yt3.ggpht.com/MUKIiMdee_7RVmqy4FoPqkBzlfMdB2dWEGxunz3DcxkUutBQlpF7nHui-Ta4HSMBIMS0hFw-W5uy=ns-nd'},
    'UCbvAoMrtqvdfDoFZxVJmS-g': {'name': 'ABC 6', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Vx4sHgcrXjhz6WF_WWcxO6s_8lJoMwE6MkjCBk239gG9BYcUpCY28YQVMW3NtwlY7j9Na9KaodJgcw=ns-nd'},
    'UCc3fJACJQkOzf8552bdIghA': {'name': 'ZEE Channel: &TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/_rwt1ovGN_GRNLN1wrwu8-wYMbU5moRLyMvAjftBIj08PTr2eS4FR-NdD88zhOYj_gXlFcS715MCLg=ns-nd'},
    'UCcHKBCoR3g3jorhhRgdrjRg': {'name': 'The Weather Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Kce3lhb1EwNLB0ajuKWV2W_F5TXk-P70RB3Z2zIpsaJvCx6opOd-DA-Olx72o6uxHmhdTHHk22o=ns-nd'},
    'UCcZNGZM43Gfl67_nGQjVZ-A': {'name': 'Pets.TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Zbil9B3uhQwD61ijk7DA-JTTDzy8l1Xm__ojLAZhaTyDZtveEiG9IM2yvywWURtEiTg-x9qC47or=ns-nd'},
    'UCdj7clRJstl32igUku_7Jaw': {'name': 'ACC Network', 'gracenote_id': '124806', 'logo_url': 'https://yt3.ggpht.com/WtLnNlZoxILqO-Ssv8nYMYbnt055xgHF7mxVaegVUZEG__gFKA6-H9MAF5Nal0yNPBA-Uie0nO8=ns-nd'},
    'UCdloZ69KU3lzw9mlNeJwsXA': {'name': 'Showtime Family Zone', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/1SG7Ws2QXDOQtwydrXd5NnFGXovZMyOwgfTfArP3BcXHgd-O6mmDNGnyNNIT-RntojHUoF9ICUe1=ns-nd'},
    'UCe12hHZUXsNfelyfz9xg0nw': {'name': 'Overtime', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/p4lpacnJOQigi4aMn627vOd-8_G_kraQQJrbmx3MY5vG2diYp28vnrklKjbs982InfBILPr2MjU=ns-nd'},
    'UCeOCNQt5UYSg38RTvrz1wAw': {'name': 'ESPN in 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/j_zbwJTebSFFD5DOOsIs_NO9IVEI3hxtAAIa20-M_qMEYkLmhKWGMO3om02pCUn57-Tgw4jVP4m7=ns-nd'},
    'UCed9LOJZCWonC04hDB3H7xQ': {'name': 'Bloomberg TV+', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/pMmuyWhNx3fitPqM7ZxicoOuhQfIpdzGzXgUCy_dP4SdhcxGca-oSgu3kvRQmVU0cUNaRygN9ojzZQ=ns-nd'},
    'UCevSLUtapg5P80Eu2WIDzdg': {'name': 'Nat Geo Wild', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/xisIEC_0MPWjIr8G1OCfDIl1XaiIF_spi8dStNsLiEFhYav49TaBoXKtQbrBn-Lkg-mcZ8F4IaAh=ns-nd'},
    'UCfMg7kRbNcYIpTRhWBmRAtw': {'name': 'ScreenPix Action', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/BvUsV87bcjuLe5z_EBgtyHd2cqUUr3y1f8tsmhX4EIC17CZn0hP3xjSbh2XRdHHgM59jAKpBsKjH=ns-nd'},
    'UCfyeDLNnqxH8OIo2fFoo2qw': {'name': 'AWE', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/8B22_DabQktx36FtcyChAv23KdqYsBBplDs8UEXSQxpMw5Dxw6SLXwpXp_D41Rn-AR9AOw-J-Hc=ns-nd'},
    'UCg-haYsC5PKaSrTho0VQeDQ': {'name': 'Telemundo', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/PA7a3-tDratk8uH1jEk2dO6-OUxN3t4s-dWVQFyo0_SriY_TYzn_IqcJG_Vba--IIZEGsnmWXnw4=ns-nd'},
    'UCg9HxR0xq_8cb08nds3h6XQ': {'name': '10TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/2a7jT0OWZMnQlDIwNzTC2Z_k-RZw8BDm842jQM6FWg9DwIIQiBPGruO4inleodSo2eCyZoV7-tqw=ns-nd'},
    'UCgAaOBjobOPnRRACWlMn30w': {'name': 'BET', 'gracenote_id': '10051', 'logo_url': 'https://yt3.ggpht.com/WJOyTE_1gFraoU5ZQA2JojcGnp0IJh_Knj8TdlSVd9BBMl-exFZLCsTrTRa0SkhdPWZo5ym5UqKy=ns-nd'},
    'UCgIhjvCt1We6i7oFjy242Xg': {'name': 'ScreenPix Voices', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/QykwfSWIrphH304uK2js9GOojsKr6ad1IdXIfTAqiPi3XpZHf2-kiHQvXPuKCSKgnZuCWDxw8Of4QQ=ns-nd'},
    'UCgLwjiTwZRuiiEfvZXjSZBw': {'name': 'TYT Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/VV7gA6cFfA8tc5BR4sp63ar2oqpIO93uL9rEDlZwwtNgDNIoSz38Qsmh18i9gMs9lDQrSxC6ul8=ns-nd'},
    'UCgTSem_UW-4aayO03h3NQFw': {'name': 'E!', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/gSaFzyUTm_VNiaPT_Gidk3JSYesiAc3eESnnj6AY1pRL1j8FGXk3yBgb-4pmMDobbvOnOuxIyueSZA=ns-nd'},
    'UCgmvjGfEnjC7nHiHk2qqY3w': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCguDPkNwA5sSy3C3upoKGDg': {'name': 'IFC', 'gracenote_id': '14873', 'logo_url': 'https://yt3.ggpht.com/WHeckZV8lc3D6djx8AqAQ6xAonBgqV1f0KHQjTFqUVQqKT86KhoBWYf5j0wri6Zn2MNbrq0qaEuL=ns-nd'},
    'UChAbpGpJzppYHFhHflFvTQg': {'name': 'ABC News Live', 'gracenote_id': '113380', 'logo_url': 'https://yt3.ggpht.com/TyJcQ2rmDG-dC3MwONAWxYTey3tVoCdgBfcEwV0l_FcsqXuuYdBJA-m1T9rMjZFVcMsocwwh98TZ=ns-nd'},
    'UChrc9SeQlK7BdkA2EL8kHMQ': {'name': 'USA 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/M10v1y6FYJNuCbsaFhl7q6AmxSOFd5rYgXBcWsG7TrHXRZvAc4W5s9-jwMyCSTlnga_QL_QxbsNzfg=ns-nd'},
    'UChukTzuQDPENsI9IYCvDEaA': {'name': 'ESPNews', 'gracenote_id': '16485', 'logo_url': 'https://yt3.ggpht.com/Kk7H3buco9r6BhwouXzwSabe9saK6N7wohMVuvuae6k4ZYoXLNg_FxEG8CEC_XJ5vXVjDIqFDakFdw=ns-nd'},
    'UCi18g8O_JtiDWb5JfbwTvqg': {'name': 'HBO Max 2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/LisjMbjSjmeNnstzS3JwG_1jt-22TUP3D_Gs3_97uixdgO1S_m7vV6gAM0RrP42i77kz6psFmAvY=ns-nd'},
    'UCi2WWF4AMSF8vvNjMrBvDgw': {'name': 'Hallmark Family', 'gracenote_id': '105723', 'logo_url': 'https://yt3.ggpht.com/ukxOSvd_hVLOXaPDrNixNcyL0WIqU6VT9Hp2pIXc-bGwPm8n66RVi97bsDC8-4wzsTMr5pyo3v1JSA=ns-nd'},
    'UCiG6vqJXFHxaLfcyvy_0CHw': {'name': 'HBO Drama East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/fwN5eTK7wYe5SIpn6mjxmvF5yXHFwfBYeuyP8Ps3zoH4sVyniHFlYjYBq0pg-Kxi_tv02LZ_T3JM=ns-nd'},
    'UCiiV9Ifjzxq1R0VhMs8DmOw': {'name': 'Stories by AMC', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/4nB2fyte6keCXCbzUVTRqAlj7194l3aUB53lVqFVZWoZWx74WhKQPzSHkJKuFz1D04WdQnqihngp=ns-nd'},
    'UCik6qJnVdzpvs5cShUCASbw': {'name': 'STARZEncore Suspense', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/sau6JjQnEE5t62QzxZ2wq9wIkox05ma7dssXpBdLJRWlnh3I0kt1s1ULyFEAxy9DSYsvYWxcUAYo=ns-nd'},
    'UCjXP478i47VpkeggT0cXHtA': {'name': 'Zee Marathi', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/JoPmJ-HqhSwwGcZ7Q_JyrY3O7To4sMzYRDJLO9HcmOF7JlMnMZc4WGMnupRidtJ8zx7XEPLldy-A=ns-nd'},
    'UCjeeSYEak0yoYOPXtKlLjOQ': {'name': 'NBA TV', 'gracenote_id': '32382', 'logo_url': 'https://yt3.ggpht.com/lwNmuM6cV_kH2dT408Q5HBdKCf7gKM2A7yh_cclROgG5urRy9qvfoSsUhkUZX94xH6rg9m4bAw=ns-nd'},
    'UCjwQspjtRywSoImMpdFiaXw': {'name': 'BBC News', 'gracenote_id': '89542', 'logo_url': 'https://yt3.ggpht.com/GFnkZ1Z_eM344vtkg5WgHbfnABkcZ0ZaM_yS8AYrqZAINAH1sVmraKcGlFQ6Qmh6uAXnBbIbgA78=ns-nd'},
    'UCk1VlyRGl0MblDM9X1_h7zg': {'name': 'Willow 2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/TLFanoGiw5qdPf9_zhRPQkFL97RoHFYhvspkyP9a0AyZSvM_bJ3cTyhsGcNT1DCRQxxkTF_pW-bY=ns-nd'},
    'UCkSrPld-jCf9vhQlP-2mUxA': {'name': 'Zee Telugu', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/i1Azjrx-H27EaypPq5mgNPM3E_U7abNKE3FUD_rbUi_YEgn2ryajZrWil9T21-LBh3apjzvAbBSc=ns-nd'},
    'UCkeQWRXklhOe2gHCujdtDfg': {'name': 'ESPN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/-PPMVXOqSukxZyOZZGHm6sT-LnZ4j_4jEelo6VXckHo4uOrdvBDALARLNl4OcuUp6bynGHFKTbz6=ns-nd'},
    'UCko2QKmtBtKuh6KoKVDZWtA': {'name': 'TV Land', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/yHiyyLtD5IMMADKJpdRK1Rpl5cNZKPhLl8u66dOD0t6CgEvCe23QGKN1UZsMnNeKFwC1eLTcgPtITA=ns-nd'},
    'UCkteTgPnQlVGOEqd6Qp6-iw': {'name': 'RACER Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/zsa4tw3HCTn3js5hCU4ij0tC7iO3WPMi3wibjjEtzCEvWpjOQHfaUKpwi331ai1NsZ9Lw5vRpYaS=ns-nd'},
    'UCl7SHXEgznwaw_X0jsZFEcg': {'name': 'ROAR', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A2L85Wu15bpVnx6IfnuDFGNzQZInXd9oc8V79TO2G5JyelDbN3HTurKOryGpX36vqQW5qr6OyFUU=ns-nd'},
    'UClCsq6O9azG2Wki7fA133wg': {'name': 'GLIV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/xESqV0NCfzJpdV5T1vBiKVFMfziw6wmmbABsV-Q7toyw-FV3pTisTWNddOoVdtjEzTpBUdgISw0=ns-nd'},
    'UClJKvIp41p37bGJOPWEO2Og': {'name': 'NEWSMAX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/V4NZf9bpFZ12zFbCpAEcEcdhLYKycIAp6aCZ8RbimcQw-VopHwRIlR1a-JfdITZF3y2aDhwnXS79CQ=ns-nd'},
    'UClLiyqVgOVfuZxaKUHSCLgQ': {'name': 'MGM+ Hits', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/03EirdfNDGlhSZWg8LJRvu1-TfriBrfG-MbcQfsOOrPqG4Pwh-XC8iZpafFZ91kjzr3l2ffDX2N0=ns-nd'},
    'UClQPfdwgE1sF5CWYLXGctEg': {'name': 'Cinemax Classics HD', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/oJdhTvMdMBDHrpud3CDgR4pDWe88idOLAMPreoitW4_3VZsDuLJXjjVGdjcS7Up5Rcx2ATmlMiA8=ns-nd'},
    'UClxYtgBSuQzBdpIsGHXJx5g': {'name': 'HGTV', 'gracenote_id': '49788', 'logo_url': 'https://yt3.ggpht.com/yIe0X-W_LHm4HtqxH86Kuer16MbbHd5MIfEUk7NW5YyA3DiVwBWQyisdymGFub8Cp3GXkSDM_2Y=ns-nd'},
    'UCm20W8G3leJQev6jDzW2WpQ': {'name': 'C-SPAN2', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A3_vfolh7ao_3aK6hw1Exa2eKL8Uko4ra5kU0LoBLHE1Jk7TNoyFJ5n94OGyluDTcVcSoXE6qUKY=ns-nd'},
    'UCmBskGxtl-7At_lMzyLJYKA': {'name': 'BTN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/pHhHDzguLkE4G6H4J6HRMBf1BPwOcqmPpt1__VPdt_O00XqXxYdHCBXf2Fx5XHVjNAOA4H5q9mWH=ns-nd'},
    'UCmZBH8fGHXyB8kRseSnmOgg': {'name': 'beIN Sports', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/-nYW5uhnTMDj0JgVCN4RkV0tZw_nJcE2cpe72tM_pH-nf-_0KYWPuRdInmOWIflf41BIcdwdSn0=ns-nd'},
    'UCmaPhYJOxFSETzGlJzZytpA': {'name': 'QVC', 'gracenote_id': '60222', 'logo_url': 'https://yt3.ggpht.com/gX7pm12u5LJhPMu7oZDIkYIwxz8-y84X4W9HLyUw7dziZcr7zUIBFDBIy7rpB7L1dVRcNrehG110-Q=ns-nd'},
    'UCmzl5oTI37uvXbAv_UN9NNQ': {'name': 'Game Show Network', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/FeODBxW0VHjkhA7HXw7-qbJZ8WW_QciJcDiKarM7wvrTigKHq8sXiBY5kL9oqGI3sD1_yMtWqg0OCw=ns-nd'},
    'UCn5onClQ2j2KyKOfmoyjyhg': {'name': 'WSYX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/THKrH3hjgYXxRsX2_TpP3cucKWpPON1_6eLol_Gu5HoB4ibHYCBgycvmkpcGxO3X86Qzvhhw450=ns-nd'},
    'UCnkxpCddkyNVVt5VCXHXw-g': {'name': 'Cinemax Hits East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/4Q56sWsnNQPr4GpvwILiRg2DD9_jXD8D6aYuBF2zVXK_jj5NHTmWz_NVH4NPDeORgbAldAb1m4Y=ns-nd'},
    'UCo7CECyCput7V2rxJwt7uBQ': {'name': 'VSiN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Wnilg7HNNGt6pNu_-HfSJdexu4iTXHg7NJ--CxVK12hsuO589fIzNq176ErpW--VhqjhnmpDAf0=ns-nd'},
    'UComo7105e_nqubnARiG7hjA': {'name': 'STARZEncore Family', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/9RaGbCfrYwIw3Vam4VcTNoBRtf98HurzfbP_1VZQeSPkKUplFUf68EElITE1epMkzWisO6XgbCYS_A=ns-nd'},
    'UCp5PQrmM0FmLtn_ZCSrL8jg': {'name': 'STARZ West', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/mdq6N2nR2B2qEXlip51kXXSQixrAAn2CyNhFP48PAMA3wTj7Kdd2z-RTXtP7Ew2xT-Pu7towmEJ1=ns-nd'},
    'UCp9TSEVSOM5TrZR1xyftSDQ': {'name': 'truTV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/Tc_OSeUWv7H9cSR1ljKyCZReze53UJE6NCl63DFUznCxRf23PFhPSblh4NcA2QcWYE1AHnwIYS91=ns-nd'},
    'UCpdAKAdxFbMGoZ4ufO-HHnw': {'name': 'HBCU GO', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/K7p7K-OA9UJqy3X5N4YPGRQTb6QDIl7hU7814mk6KMVRGSV9D6_pzDhTo3qQwcpDH78RjfA4DGOy8Q=ns-nd'},
    'UCpj5ieQ9I6GFSsRnON4AOBQ': {'name': 'SEC Network', 'gracenote_id': '89714', 'logo_url': 'https://yt3.ggpht.com/hAVXZ95QyA6B5ELlJ3ZjxIBmv61Y7RAgkN6DQZKdGUIeBd8fak7pcgUYtkrE-RN1_i0dnkV_pbHT=ns-nd'},
    'UCqC6dk2TYYUZCmHj_l8oycw': {'name': 'IMPACT Wrestling Channel', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/R-iTTZ73IRuxOIgf0BtySkpmLxcFWWHhBAehIfBm0QjGIkOWydDvcA25z4wUEvIGqVcdJsg-nw-9ng=ns-nd'},
    'UCqIassGhNdIv5Bf6giTu6AQ': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCqMR2sySVw94zZHaNqLXsfg': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCqWYIzQLZFW4NFXPfWBLW1w': {'name': 'FOX News', 'gracenote_id': '60179', 'logo_url': 'https://yt3.ggpht.com/YwzCgGcjCay_O89TiAYoP7mRSVS_KewMlxk8MlHxKsXzTQum2L3PxXkHZjb0GKG38Z0COFEzCbMQ=ns-nd'},
    'UCqYjz1ylKY6C9ycHaOsVigA': {'name': 'NFL ST - FOX', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCqhyiwRnWi8k8BVuffvoRSQ': {'name': 'STARZEncore Black', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/kA7HoaPkUAqME2DKsVYmfQCh8Woyf1-4hb7GPO2wiu2z26d1iK1yKiEbM7cuKQ9Zl-r9wzL0xaOX9w=ns-nd'},
    'UCquxTSBuMAzHJT_n09tNU6Q': {'name': 'NFL ST - CBS', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/A8ygOdN60Yuyq15jhVmP8tmwqsQtdWKLLEGQ5PTP1WoWYsNF1KNcRzMFwywvA5Ct2Baph4VR2Js=ns-nd'},
    'UCr4BJp-pkGTiG-8AkDn2Qug': {'name': 'Zee Keralam', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/3oktBlXJPRTihZ4DOzTmZ5_Y17RNTdjbtCPS6XdXW5NFyW8-oOQo2nbF_Q-9v2ntuLH-buQWCVTG=ns-nd'},
    'UCrD_FFXOHxaO4AiWZ1q2HGA': {'name': 'Zing USA', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/sugYhMx-AbPUd0mS4k-EqsA6j7nbKbVQjca--FhS5Sne7w98VjBS8AhJPqrauBqoty_WLIDPIJ-N=ns-nd'},
    'UCrxOU69oUq5Pb8oneCtTIyQ': {'name': 'Hallmark Channel', 'gracenote_id': '66268', 'logo_url': 'https://yt3.ggpht.com/-SIcEbgEzolmioayJCqXkvnZq0ZdPik1fxi7ELLvwqEu0dz-4sl3xB_EvEshTPBi47cnzwvkS9VV1A=ns-nd'},
    'UCsIuB1rNY2tATlP1XjSbCvg': {'name': 'FOX Sports 4K', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/iomJWJGkpH-DBcDQrodQfGLeqCvKlV8CL6YXAlxO2aR3ey_r2GM9rx1flKQVgKs74SWGb9xLM3L_=ns-nd'},
    'UCt9Uk3laU-lBz33BtEN242g': {'name': 'Billiard TV', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bsX_u9yTdJSA8lRG9G5Y1qnHGpUbE_0LHiji-gVJbbRd5Bs9ySan_TNNL1CCqaEA-_2KSELFO-8=ns-nd'},
    'UCtF4NnSq2b6Nkv_Q-7PVYWw': {'name': 'Zee World', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/otRy917zgQgEo8iFkx4xLy9PbUt756qFFi48USQLVuIjbXOENY0AK2OOs1IqWvvY7wvEroK11PEb=ns-nd'},
    'UCtGP7XtIzd3bwARBmJNcZYA': {'name': 'ESPNU', 'gracenote_id': '45654', 'logo_url': 'https://yt3.ggpht.com/Eep5r-kg7T-GnAhEXfW1FFgLFZYMtzKSBkSO2Tssjs5lerjQwgAlmJTdo_nKOemRpmB570gzUz9AYw=ns-nd'},
    'UCtZliRrQHs5BP_TFbZ9iXHA': {'name': 'NBCSN Extra', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/jOWm8tOUk8D70Ogl_t1PsppiVGCqXHKNIFXKUcblzSLrToYfz-wFPaW0VZpJRSKdKOCn0CVxUKvM=ns-nd'},
    'UCtfoIx_MZ0h5bX3i9YsF5xw': {'name': 'YouTube TV Zen', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/e8hDzj3NmJmYwTjaPM2vuDc14EojVviTpczZK_Ct4D-uf4MJH9fQqOh5zaK93eLqjJC306CC-jQ=ns-nd'},
    'UCua8qoW-BF5-7TGAv_G7pDA': {'name': 'NBCSN Extra', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/W08LVhmccoutfIdVnr9b-HuaRyQ0R-zR6dp3O4QEkmBgL3kpHS3jYVgSAnv4wheS9gAliL7yz3H51Q=ns-nd'},
    'UCumdadB3EZECXspnGutJNEg': {'name': 'Showtime Extreme', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/t54YoW1Ri8Ma43-vfX1cUlvEFo6Q4RZz1QXVWHgNZANNlYCuDqSiu0S_xMmxKBAh00588p0YFmKC=ns-nd'},
    'UCv15E33qNOnHUKi188QZ7Aw': {'name': 'BBC America', 'gracenote_id': '18332', 'logo_url': 'https://yt3.ggpht.com/_p-1X5yQLoIEMAncxkXAZg-VUaFmr6cajdCO5Yk1aOIh-FMXpkjZgYReTfpijSvoOa2QK8t4iU_vRQ=ns-nd'},
    'UCv7MjK9rUgQvLnB_ZPihABw': {'name': 'ESPN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/6o7EXpxOyA5dp5cRFXYeLA9F9SLvVvPI0xmGW58l3CQ2dFAIUupjDwkd1P1HMMOznYXC2tA2GIpz-g=ns-nd'},
    'UCv_pZhnIyyPj3jf9TS9uToQ': {'name': 'Freeform', 'gracenote_id': '59615', 'logo_url': 'https://yt3.ggpht.com/WQq_vGgSk5C234znlagh9xeXDsS7ea4bUuL_LoN4sYAe8h3bYfXFPOOrD2B6kRGB69tdTyCHAyY=ns-nd'},
    'UCvaTleka5KrZxTWcmFFUpjg': {'name': 'ScreenPix Westerns', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/ysi823L98qZZgXda7cHYhsm-KVsW6dxlSzm4cZrGR4RT1qsFYUE1tHSrJ5pEvZl41TVTOntcjxml=ns-nd'},
    'UCvd5mBfn5QSez1f6i4F-dQg': {'name': 'Showtime East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/tx5iZCutGgiooDuOcmryyNn5Mmx_a_EpgzHauwyH-XjHyHSRr0z_91-UVAxNwIEQy2xXWqQkDk7O=ns-nd'},
    'UCvtIF-MwpMBXKsbYZvuHpYg': {'name': 'Tastemade', 'gracenote_id': '107076', 'logo_url': 'https://yt3.ggpht.com/I3I0ofKRm2_h_bqipi2B4q1x9yxXGXTceSbRubNzwBsYEQrWcozjK_PQ-qtYWnCSjMXHQIfWB24k=ns-nd'},
    'UCvx0APUm4CuRszt9BRNA9TA': {'name': 'FOX Soccer Plus', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/bbiOV2wlb3W-VkZrj2qFytCqAoE2c4idgocF_4l3g7fdW7J-au2-cnXtFfbBMpGZhWZERY-yLv_g=ns-nd'},
    'UCwCx1zNO1Qd4BHq3Jjl064Q': {'name': 'Disney Junior', 'gracenote_id': '75004', 'logo_url': 'https://yt3.ggpht.com/C52M1SVfpPOQcQKjnpgsoPexhBFqtxgy4s4rV84uBQ8wKpMH3b4K9LCenymtMThy3G0uZnBm6RcjuQ=ns-nd'},
    'UCwOohtHXo0hkQMB4WO3Ib8g': {'name': 'Food Network', 'gracenote_id': '12574', 'logo_url': 'https://yt3.ggpht.com/tZT_W0Llh2USLly9uNmgT9HdPm5LqftoZU3d0-InSq2amVB27fyHIlAiYTKBZ66bvyt0V7ZGBiJaFw=ns-nd'},
    'UCwUoTw92Bghbi86Mua1jM6Q': {'name': 'STARZ East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/8NbDieCBMKkCXs1wTbckEVZTYqYVi_uBiUaQJyMLjE4gHO7caWicVR4wTr9uNLuRToounkoFkRoh=ns-nd'},
    'UCwig0jo9FYVff261Pd05MgQ': {'name': 'Comedy Central', 'gracenote_id': '64599', 'logo_url': 'https://yt3.ggpht.com/kXan2mYCX4kqrNj8EDS0DY1MKmNYjMawhFwOod1UHD3-TocltIKsw2ayaSyLZAKb4r6Z9feIs1U=ns-nd'},
    'UCwmtfDGiiipObmIEi37e9hg': {'name': 'Hallmark Mystery', 'gracenote_id': '46710', 'logo_url': 'https://yt3.ggpht.com/-rPyJTg7i55Dnhsfah4dTR41Bsxs9NwNqo9OyMJJaCrdv38aV0pq3jObDj9xAagi8hsC0xNgqaoP=ns-nd'},
    'UCxXjdrevcsy6xh0_O3KhokA': {'name': 'USA', 'gracenote_id': '11208', 'logo_url': 'https://yt3.ggpht.com/ecqrjoU5NEmiMAsidd4IcAcVLOVlIU3Lefwgop2hw2eVXvCleVgZUmKwdcAGPHwn-wEQps8NumPGxQ=ns-nd'},
    'UCxlgoUf5jJaGXbH_GaD4G5Q': {'name': 'HBO Latino East', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/kbc9cRh6TGNWoSTpRNF7hD8C_Xrbv7_eqgjTyxvojt7ebg_HlrKmDEh37tkuIiUVu-jw61On-WaF=ns-nd'},
    'UCyc2_6L6fzbz3b9DVhxIpGQ': {'name': 'ESPN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/iijKzZaINRMSPlbx_mD47aoMrEPsM2U4-6MDfogKtpWeLX55TyFtzcbAnD071nJaDmnhAnF2-q8=ns-nd'},
    'UCyeti25JXK1zbcL33QtJ2zg': {'name': 'True CRMZ', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/dVNXrhE1N0zORgbZbHAnGFBmjk18NGEVqY7XnatDBM0QZbifj7Z4MA21tvGmja6Qjnl8n4VksYmIIg=ns-nd'},
    'UCyn1QNRFEptWzFMqcXDbndg': {'name': 'GFAM', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/CvCJEpfaBMfW0rPoHR12XTJyindkW7X8ZOuTGy0iMHXiouHO4-aNMNT5c93Lwwal9BeFiyFYVzg=ns-nd'},
    'UCyphw3Ek3Ta6iahP_X0wFQQ': {'name': 'Alpha ETC Punjabi', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/cX64NUWxmp-T_RAPmFS7sv4zUOz7im4ktwzdQuV4RAUP2Ytij1swFTSG1tLE_aWbMyc-oRE2EOE=ns-nd'},
    'UCztf61frc248u2S_AMhcMbg': {'name': 'C-SPAN', 'gracenote_id': None, 'logo_url': 'https://yt3.ggpht.com/6ZKjrZQaEOi9OMQ0V4DkJWF9pBOMgmwF3163KjX1RwacpgowlSMXnApPfIupd_Ag5YCCWcVrWst2Wg=ns-nd'},
}


class YouTubeTVAuthError(RuntimeError):
    pass


def load_youtubetv_google_master_token() -> dict | None:
    """This scraper's OWN Google master_token, stored on the youtubetv Source's
    own config — deliberately independent of the TVE feature's TVEAccount row
    (which has its own, unrelated master_token for YouTubeTV-as-MVPD priming).
    Someone who never touches TVE can still configure/use this scraper; see
    _bearer_token()'s TVE fallback below for the one deliberate point of
    sharing (reuse a token that's already there, don't force a second login)."""
    source = Source.query.filter_by(name='youtubetv').first()
    if not source:
        return None
    return (source.config or {}).get('google_master_token') or None


def save_youtubetv_google_master_token(data: dict) -> None:
    source = Source.query.filter_by(name='youtubetv').first()
    if not source:
        return
    cfg = dict(source.config or {})
    cfg['google_master_token'] = data
    source.config = cfg
    db.session.commit()


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
    config_required = True  # needs its own Google sign-in done first (this source's own config panel)
    under_development = True  # channel/EPG discovery only, no playback yet
    scrape_interval = 720
    stream_audit_enabled = False  # nothing resolvable to audit yet

    def _bearer_token(self) -> str:
        saved = load_youtubetv_google_master_token()
        via_tve = False
        if not saved:
            # Deliberate one-way sharing: if the (separate, optional) TVE
            # feature already has a Google sign-in on file, reuse it rather
            # than force a second login for the same account — but this
            # scraper's OWN token (checked first, above) never requires TVE
            # to be configured or enabled at all.
            tve_account = TVEAccount.query.filter_by(provider_id='mvpd').first()
            if tve_account:
                saved = adobe_pass.load_google_master_token(tve_account)
                via_tve = bool(saved)
        if not saved:
            raise YouTubeTVAuthError(
                'No Google sign-in on file — use "Sign in with Google" in this '
                'source\'s own config panel (Sources -> YouTube TV -> Configure).')
        token = mint_youtubetv_bearer_token(saved)
        at = int(time.time())
        if not token:
            self._update_config('google_signin_last_check', {
                'status': 'error', 'message': 'token mint failed (revoked or expired)', 'at': at,
            })
            raise YouTubeTVAuthError(
                'Google sign-in on file could not mint a YouTube TV token '
                '(revoked or expired) — sign in with Google again.')
        self._update_config('google_signin_last_check', {
            'status': 'ok',
            'message': 'minted OK' + (' (via the shared TVE Google sign-in)' if via_tve else ''),
            'at': at,
        })
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

    def _fetch_live_station_info(self) -> dict[str, dict[str, str | None]] | None:
        """Live per-scrape replacement for the baked-in _STATION_CROSSWALK's
        name/logo data (gracenote_id has no live source — always comes from
        the static crosswalk). Same technique as the one-off 2026-09-16
        capture that built that crosswalk in the first place, just automated:
        replicate the real web client's own authenticated
        browseId=FEunplugged_epg + epgOptions call directly via requests,
        using cookies read from the signed-in guide profile
        (_YOUTUBETV_GUIDE_PROFILE_DIR) plus a locally-computed SAPISIDHASH —
        the same auth scheme the browser's own JS computes, not anything
        defeating a protection (it's this account's own already-authorized
        session). No browser render needed, ~3-4s total. Returns None on any
        failure (no signed-in session, expired cookies, network error,
        unexpected response shape) so fetch_channels() can fall back to the
        static crosswalk — this is best-effort freshness, not a hard
        requirement to scrape at all.
        """
        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            return None
        try:
            with Camoufox(
                headless='virtual', os='windows', persistent_context=True,
                user_data_dir=_YOUTUBETV_GUIDE_PROFILE_DIR,
            ) as context:
                cookies = context.cookies()
        except Exception as exc:  # noqa: BLE001
            logger.info('[youtubetv] live station-info: could not read guide profile cookies '
                        '(not signed in yet, or profile busy with another job): %s', exc)
            return None

        cookie_dict = {
            c['name']: c['value'] for c in cookies
            if 'youtube.com' in c.get('domain', '') or 'google.com' in c.get('domain', '')
        }
        sapisid = cookie_dict.get('SAPISID')
        if not sapisid:
            logger.info('[youtubetv] live station-info: no SAPISID cookie — guide sign-in '
                        'not completed or session expired, falling back to the static crosswalk')
            return None

        origin = 'https://tv.youtube.com'
        ts = str(int(time.time()))
        sapisidhash = hashlib.sha1(f'{ts} {sapisid} {origin}'.encode()).hexdigest()
        body = {
            'browseId': 'FEunplugged_epg',
            'unpluggedBrowseOptions': {'epgOptions': {
                'maxAiringsPerStation': 1,
                'initialEpgFetchStartTimeMs': str(int(time.time() * 1000)),
                'initialEpgFetchDurationMs': 60000,
                'paginationDurationMs': _EPG_PAGINATION_DURATION_MS,
                'maxDurationMs': _EPG_MAX_DURATION_MS,
            }},
            'context': {'client': {
                'hl': 'en', 'gl': 'US', 'clientName': 'WEB_UNPLUGGED',
                'clientVersion': '1.20260913.03.00', 'platform': 'DESKTOP',
            }},
        }
        try:
            resp = self.session.post(
                f'{origin}/youtubei/v1/browse?alt=json', json=body,
                headers={
                    'Authorization': f'SAPISIDHASH {ts}_{sapisidhash}',
                    'Content-Type': 'application/json',
                    'Cookie': '; '.join(f'{k}={v}' for k, v in cookie_dict.items()),
                    'Origin': origin, 'Referer': f'{origin}/live',
                    'X-Origin': origin, 'X-Goog-AuthUser': '0',
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.info('[youtubetv] live station-info: request failed (non-fatal): %s', exc)
            return None

        try:
            rows = (
                data.get('contents', {}).get('epgRenderer', {})
                .get('paginationRenderer', {}).get('epgPaginationRenderer', {}).get('contents', [])
            )
        except AttributeError:
            rows = []
        if not rows:
            logger.info('[youtubetv] live station-info: response had no station rows (non-fatal)')
            return None

        result: dict[str, dict[str, str | None]] = {}
        for row_wrap in rows:
            row = row_wrap.get('epgRowRenderer', {})
            station = row.get('station', {}).get('epgStationRenderer', {})
            sid = row.get('stationId') or station.get('stationId')
            if not sid:
                continue
            name = None
            for key in ('name', 'callSign'):
                runs = station.get(key, {}).get('runs', [])
                if runs and runs[0].get('text'):
                    name = runs[0]['text']
                    break
            if not name:
                for key in ('icon', 'secondaryIcon'):
                    label = station.get(key, {}).get('accessibility', {}).get('accessibilityData', {}).get('label')
                    if label:
                        name = label
                        break
            if not name:
                continue
            thumbs = station.get('icon', {}).get('thumbnails', [])
            logo = thumbs[-1]['url'] if thumbs else None
            if logo and logo.startswith('//'):
                logo = 'https:' + logo
            result[sid] = {'name': name, 'logo_url': logo}
        return result or None

    def fetch_channels(self) -> list[ChannelData]:
        bearer = self._bearer_token()
        station_ids = self._fetch_station_ids(bearer)
        if not station_ids:
            logger.warning('[youtubetv] EPG grid returned no channels')
            return []

        live_info = self._fetch_live_station_info()
        if live_info:
            logger.info('[youtubetv] live station-info fetch OK — %d stations, using in place of '
                        'the static crosswalk for name/logo', len(live_info))
        else:
            logger.info('[youtubetv] live station-info unavailable this run — falling back to the '
                        'static crosswalk (name/logo may be stale)')
        matched = sum(
            1 for sid in station_ids
            if (live_info and sid in live_info) or sid in _STATION_CROSSWALK
        )
        logger.info('[youtubetv] %d/%d stations resolved a name', matched, len(station_ids))

        channels = []
        for sid in station_ids:
            live = live_info.get(sid) if live_info else None
            known = _STATION_CROSSWALK.get(sid)
            # live_info has no gracenote_id (no live source for that data —
            # see _STATION_CROSSWALK's own comment), so that always comes
            # from the static crosswalk regardless of which name/logo won.
            name = (live or known or {}).get('name') or sid
            logo_url = (live or known or {}).get('logo_url')
            gracenote_id = known.get('gracenote_id') if known else None
            channels.append(ChannelData(
                source_channel_id=sid,
                name=name,
                slug=f'youtubetv-{sid.lower()}',
                stream_url=f'{SCHEME}{sid}',
                stream_type='hls',
                logo_url=logo_url,
                category=category_for_channel(name, None, self.source_name) if (live or known) else None,
                language='en',
                country='US',
                gracenote_id=gracenote_id,
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
