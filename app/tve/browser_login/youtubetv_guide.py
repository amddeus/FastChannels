"""Standalone "Sign in to YouTube TV guide" browser-assisted flow.

Separate from google.py's run_google_signin(): that flow only ever visits
Google's embedded Android device-setup page to capture a master_token for
Adobe-Pass/MVPD priming, and never actually establishes a real
tv.youtube.com session — confirmed live 2026-09-15 that neither the shared
mvpd_tve profile nor a master_token-minted cookie set gets past
tv.youtube.com's own login check (redirects to /welcome?rd_rsn=lo, i.e.
"logged out"). This flow exists specifically to get a REAL tv.youtube.com
session on disk, by navigating to the guide itself and doing a real
interactive login there.

Uses _YOUTUBETV_ISOLATED_PROFILE_DIR (/data/browser_profiles/youtubetv) —
the SAME profile run_youtubetv_google_signin() below (this scraper's "Sign
in with Google" step) also uses, deliberately, as of 2026-09-16. The two
steps used to live on different profiles (this one here, "Sign in with
Google" on the TVE feature's shared mvpd_tve profile) with a cross-profile
"priming" call bridging them — confirmed live 2026-09-16 as a real bug: it
silently carried the TVE feature's OWN saved Google account into this flow
with no login form ever shown, making it impossible to switch this scraper
to a different Google account than whatever TVE happened to have on file.
Now both steps share one dedicated, TVE-independent profile: sign in via
either step and the other naturally sees the same session already there —
no explicit priming code needed, and switching accounts (wipe this one
profile dir, sign in again) is unambiguous and complete.
"""
import logging
import time

import redis

from app.worker import flask_app
from app.tve.browser_login.common import (
    _GOOGLE_SETUP_URL,
    _YOUTUBETV_ISOLATED_PROFILE_DIR,
    _YOUTUBETV_CAMOUFOX_FIREFOX_PREFS,
    _autofill_google_account_chooser,
    _safe_page_url,
    _relay_input_and_screenshot,
    _BrowserSessionDied,
    install_browser_login_activity_log,
    uninstall_browser_login_activity_log,
)

logger = logging.getLogger(__name__)

YTTV_GUIDE_SIGNIN_STATUS_KEY = 'yttv-guide:browser-login:status'
YTTV_GUIDE_SIGNIN_SHOT_KEY = 'yttv-guide:browser-login:screenshot'
YTTV_GUIDE_SIGNIN_INPUT_KEY = 'yttv-guide:browser-login:input'
YTTV_GUIDE_SIGNIN_STOP_KEY = 'yttv-guide:browser-login:stop'
YTTV_GUIDE_SIGNIN_HINT_KEY = 'yttv-guide:browser-login:hint'
_YTTV_GUIDE_SIGNIN_TIMEOUT_SECONDS = 600
_GUIDE_URL = 'https://tv.youtube.com/live'


def run_youtubetv_guide_signin():
    """Real interactive login against tv.youtube.com/live itself, so the
    persistent _YOUTUBETV_ISOLATED_PROFILE_DIR profile ends up with a real
    signed-in session — needed for app.scrapers.youtubetv's DOM-scrape guide
    read (real channel names/logos, same technique as PrismCast's
    youtubeTv.ts) and possibly richer EPG. No token to capture/save at the
    end — success just means the guide rendered; the profile's own saved
    cookies are the durable credential from here on, reused headless by
    whatever calls scrape_guide_dom() in app/scrapers/youtubetv.py.
    """
    _ctx = flask_app.app_context()
    _ctx.push()
    _ctx_popped = {'v': False}
    _activity_handler = None
    try:
        import json as _json_login

        try:
            r = redis.from_url(flask_app.config['REDIS_URL'])
            r.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning('[yttv-guide-signin] Redis unavailable, aborting: %s', exc)
            return
        _activity_handler = install_browser_login_activity_log(r)

        _terminal_status_set = {'v': False}

        def set_status(state: str, message: str = '', url: str = ''):
            try:
                r.setex(
                    YTTV_GUIDE_SIGNIN_STATUS_KEY, 120,
                    _json_login.dumps({'state': state, 'message': message, 'url': url}),
                )
                if state in ('success', 'error', 'stopped'):
                    _terminal_status_set['v'] = True
            except Exception:  # noqa: BLE001
                pass

        r.delete(YTTV_GUIDE_SIGNIN_STOP_KEY)
        r.delete(YTTV_GUIDE_SIGNIN_INPUT_KEY)
        set_status('starting', 'Launching browser…')

        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            set_status('error', 'Camoufox is not installed on this container')
            return

        try:
            import os as _os_login
            _os_login.makedirs(_YOUTUBETV_ISOLATED_PROFILE_DIR, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[yttv-guide-signin] could not create profile dir %s: %s',
                            _YOUTUBETV_ISOLATED_PROFILE_DIR, exc)

        _ctx.pop()
        _ctx_popped['v'] = True
        deadline = time.monotonic() + _YTTV_GUIDE_SIGNIN_TIMEOUT_SECONDS
        guide_loaded = False
        try:
            with Camoufox(
                headless='virtual', os='windows', persistent_context=True,
                user_data_dir=_YOUTUBETV_ISOLATED_PROFILE_DIR, window=(1280, 800),
                firefox_user_prefs=_YOUTUBETV_CAMOUFOX_FIREFOX_PREFS,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.on('crash', lambda p: logger.warning('[yttv-guide-signin] page CRASH event fired (url was %s)', _safe_page_url(p)))
                page.on('close', lambda p: logger.warning('[yttv-guide-signin] page CLOSE event fired'))
                page.on('pageerror', lambda exc: logger.warning('[yttv-guide-signin] page JS error: %s', str(exc)[:500]))

                # Navigate straight to Google's standard sign-in entry point
                # with a continue= URL back to the guide, rather than
                # tv.youtube.com/live itself. Confirmed live 2026-09-16 TWICE
                # (once in scripted testing, once against a real user's real
                # relayed click): a signed-out profile visiting /live lands on
                # tv.youtube.com's /welcome marketing page, and its "Sign in"
                # button is unreliable -- worked once, then silently failed to
                # navigate on a later attempt with no code change, and also
                # failed to fire on a real human's real relayed mouse click
                # (confirmed via a live screenshot: the page was still sitting
                # on /welcome's auto-playing carousel, completely unmoved,
                # long after the click was sent and relayed successfully).
                # This URL is the same standard Google OAuth entry any real
                # "sign in with Google" link uses, reaches the identical
                # accounts.google.com login form deterministically (no
                # /welcome, no flaky button dependency at all), and its own
                # continue= param redirects back to the guide on success.
                #
                # UPDATE, same session: a direct continue=tv.youtube.com/live
                # reached the login form fine but, after a real sign-in,
                # redirected back to /welcome instead of the guide -- Google
                # authenticated the browser, but tv.youtube.com's OWN session
                # recognition apparently isn't satisfied by landing there
                # directly. The real "Sign in" button's own continue chain
                # (captured from an actual click earlier this session) hops
                # through m.youtube.com/signin first:
                #   continue=m.youtube.com/signin?action_handle_signin=true
                #     &feature=upg_web_login&skip_identity_prompt=True&hl=en
                #     &next=<tv.youtube.com URL>
                # That handoff page is presumably what actually establishes
                # tv.youtube.com's own session cookie/state, not just Google's
                # account-level login -- reproduced verbatim here rather than
                # jumping straight to /live.
                from urllib.parse import quote as _quote
                _next_url = 'https://tv.youtube.com/?utm_servlet=prod&rd_rsn=lo&onboard=2'
                _mobile_signin = (
                    'http://m.youtube.com/signin?action_handle_signin=true&feature=upg_web_login'
                    f'&skip_identity_prompt=True&hl=en&next={_quote(_next_url, safe="")}'
                )
                signin_url = (
                    'https://accounts.google.com/ServiceLogin?service=youtube'
                    f'&continue={_quote(_mobile_signin, safe="")}&hl=en'
                )
                page.goto(signin_url, wait_until='commit', timeout=30000)
                set_status('running', 'Sign in below, including any 2FA/captcha if shown.', page.url)

                wait_started = time.monotonic()
                last_shot = 0.0
                last_renav = 0.0
                while time.monotonic() < deadline:
                    if r.exists(YTTV_GUIDE_SIGNIN_STOP_KEY):
                        set_status('stopped', 'Cancelled')
                        return
                    if page.is_closed():
                        raise _BrowserSessionDied('browser page closed before sign-in completed')
                    now = time.monotonic()
                    if now - last_shot > 0.25:
                        last_shot = now
                        _autofill_google_account_chooser(page)
                        if _relay_input_and_screenshot(
                            page, r, waiting_since=wait_started,
                            stop_key=YTTV_GUIDE_SIGNIN_STOP_KEY, input_key=YTTV_GUIDE_SIGNIN_INPUT_KEY,
                            shot_key=YTTV_GUIDE_SIGNIN_SHOT_KEY, hint_key=YTTV_GUIDE_SIGNIN_HINT_KEY,
                        ):
                            set_status('stopped', 'Cancelled')
                            return
                    try:
                        guide_loaded = page.locator('ytu-epg-row').count() > 0
                    except Exception:  # noqa: BLE001
                        guide_loaded = False
                    if guide_loaded:
                        break
                    # Confirmed live 2026-09-16: a real sign-in lands on
                    # tv.youtube.com's Home tab, not /live, even though /live
                    # was the original navigation target -- there's no
                    # auto-redirect to the guide grid. Once we're clearly
                    # past Google's login (on tv.youtube.com, not
                    # accounts.google.com) but the grid still hasn't shown up,
                    # periodically force a re-navigation back to it instead of
                    # requiring a manual click on "• LIVE". Bounded to every
                    # 3s so it can't interfere with the Google login phase
                    # (page.url only reaches tv.youtube.com after that's
                    # done) or spin tightly if something else is wrong.
                    try:
                        # Real hostname check, NOT a raw substring search --
                        # confirmed live 2026-09-16 as a real, active bug: the
                        # sign-in URL above embeds "tv.youtube.com" as literal
                        # text inside its own continue= query parameter (only
                        # the surrounding punctuation gets percent-encoded,
                        # not the hostname text itself), so a naive `in`
                        # check matched while STILL on accounts.google.com's
                        # login page and forced a re-navigation back to
                        # /live every 3s before the user had even finished
                        # signing in -- which, since nothing was authenticated
                        # yet, just bounced straight back to /welcome. Looked
                        # exactly like "clicking the login field sends me back
                        # to the splash page" but was actually on a timer,
                        # unrelated to any click.
                        from urllib.parse import urlparse as _urlparse
                        on_youtube = (_urlparse(page.url or '').hostname or '') == 'tv.youtube.com'
                    except Exception:  # noqa: BLE001
                        on_youtube = False
                    if on_youtube and (now - last_renav) > 3.0:
                        last_renav = now
                        try:
                            # 'commit' not 'domcontentloaded' -- see the
                            # comment on the initial goto above; the same
                            # lifecycle-hang risk applies to any tv.youtube.com
                            # navigation, not just the very first one.
                            page.goto(_GUIDE_URL, wait_until='commit', timeout=15000)
                        except Exception as exc:  # noqa: BLE001
                            logger.info('[yttv-guide-signin] re-navigation to guide failed (will retry): %s', exc)
                    page.wait_for_timeout(200)
        except BaseException as exc:  # noqa: BLE001
            if _terminal_status_set['v']:
                logger.info('[yttv-guide-signin] ignoring cleanup-time exception after terminal status was already set: %s', exc)
                return
            if r.exists(YTTV_GUIDE_SIGNIN_STOP_KEY):
                set_status('stopped', 'Cancelled')
                return
            logger.exception('[yttv-guide-signin] browser session failed')
            set_status('error', f'Browser session failed: {exc}')
            return

        if not guide_loaded:
            set_status('error', 'Timed out waiting for the YouTube TV guide to load — sign-in may not have completed.')
            return

        logger.info('[yttv-guide-signin] guide loaded — tv.youtube.com session saved to %s', _YOUTUBETV_ISOLATED_PROFILE_DIR)
        _record_guide_signin_success()
        set_status('success', 'Signed in — YouTube TV guide loaded successfully. Channel names can now be scraped directly from it.')
    finally:
        uninstall_browser_login_activity_log(_activity_handler)
        if not _ctx_popped['v']:
            _ctx.pop()


def _record_guide_signin_success() -> None:
    """Best-effort status timestamp for the youtubetv source's own config
    panel — see app/scrapers/youtubetv.py's renderYoutubetvConfig usage.
    Never raises; a failed write just means the panel shows no timestamp."""
    try:
        from app.extensions import db
        from app.models import Source
        with flask_app.app_context():
            source = Source.query.filter_by(name='youtubetv').first()
            if not source:
                return
            cfg = dict(source.config or {})
            cfg['guide_signin_at'] = int(time.time())
            source.config = cfg
            db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[yttv-guide-signin] could not record success timestamp (non-fatal): %s', exc)


# ── Standalone "Sign in with Google" for the youtubetv scraper's OWN config ──
# Deliberately NOT app.tve.browser_login.google.run_google_signin(): that one
# saves to the TVE feature's shared TVEAccount row, gated behind TVE being
# enabled. This scraper needs to work with TVE completely untouched/disabled
# -- see app.scrapers.youtubetv.load_youtubetv_google_master_token's
# docstring for the full reasoning. Otherwise near-identical to
# run_google_signin(): same Google embedded device-setup page, same
# oauth_token-cookie capture -- but uses _YOUTUBETV_ISOLATED_PROFILE_DIR (the
# SAME profile run_youtubetv_guide_signin() above uses), NOT the TVE
# feature's shared mvpd_tve profile. Changed 2026-09-16 -- sharing mvpd_tve
# was the root cause of a real bug (see the module docstring): it made this
# step, and the guide-signin step above, silently carry over whatever
# Google account the separate TVE feature happened to have on file, with no
# way to pick a different one for just this scraper. Both steps now live on
# one dedicated, TVE-independent profile.
GOOGLE_SIGNIN_STATUS_KEY = 'youtubetv-google:browser-login:status'
GOOGLE_SIGNIN_SHOT_KEY = 'youtubetv-google:browser-login:screenshot'
GOOGLE_SIGNIN_INPUT_KEY = 'youtubetv-google:browser-login:input'
GOOGLE_SIGNIN_STOP_KEY = 'youtubetv-google:browser-login:stop'
GOOGLE_SIGNIN_HINT_KEY = 'youtubetv-google:browser-login:hint'
_GOOGLE_SIGNIN_TIMEOUT_SECONDS = 600


def run_youtubetv_google_signin():
    """Captures a Google master_token for THIS scraper's own config, with no
    TVE dependency at all. See module docstring above run_google_signin's
    sibling for why this isn't just reusing that function."""
    _ctx = flask_app.app_context()
    _ctx.push()
    _ctx_popped = {'v': False}
    _activity_handler = None
    try:
        import json as _json_login

        try:
            r = redis.from_url(flask_app.config['REDIS_URL'])
            r.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning('[youtubetv-google-signin] Redis unavailable, aborting: %s', exc)
            return
        _activity_handler = install_browser_login_activity_log(r)

        _terminal_status_set = {'v': False}

        def set_status(state: str, message: str = '', url: str = ''):
            try:
                r.setex(
                    GOOGLE_SIGNIN_STATUS_KEY, 120,
                    _json_login.dumps({'state': state, 'message': message, 'url': url}),
                )
                if state in ('success', 'error', 'stopped'):
                    _terminal_status_set['v'] = True
            except Exception:  # noqa: BLE001
                pass

        r.delete(GOOGLE_SIGNIN_STOP_KEY)
        r.delete(GOOGLE_SIGNIN_INPUT_KEY)
        set_status('starting', 'Launching browser…')

        from app.scrapers.youtubetv import load_youtubetv_google_master_token, save_youtubetv_google_master_token
        with flask_app.app_context():
            already = load_youtubetv_google_master_token()
        if already:
            set_status('success', f"Already signed in as {already.get('email', '?')}. Sign in again only if this stops working.")
            return

        try:
            from camoufox.sync_api import Camoufox
        except ImportError:
            set_status('error', 'Camoufox is not installed on this container')
            return

        try:
            import os as _os_login
            _os_login.makedirs(_YOUTUBETV_ISOLATED_PROFILE_DIR, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[youtubetv-google-signin] could not create profile dir %s: %s',
                            _YOUTUBETV_ISOLATED_PROFILE_DIR, exc)

        _ctx.pop()
        _ctx_popped['v'] = True
        deadline = time.monotonic() + _GOOGLE_SIGNIN_TIMEOUT_SECONDS
        oauth_token = None
        try:
            with Camoufox(
                headless='virtual', os='windows', persistent_context=True,
                user_data_dir=_YOUTUBETV_ISOLATED_PROFILE_DIR, window=(1280, 800),
                firefox_user_prefs=_YOUTUBETV_CAMOUFOX_FIREFOX_PREFS,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.on('crash', lambda p: logger.warning('[youtubetv-google-signin] page CRASH event fired (url was %s)', _safe_page_url(p)))
                page.on('close', lambda p: logger.warning('[youtubetv-google-signin] page CLOSE event fired'))
                page.on('pageerror', lambda exc: logger.warning('[youtubetv-google-signin] page JS error: %s', str(exc)[:500]))

                page.goto(_GOOGLE_SETUP_URL, wait_until='domcontentloaded', timeout=30000)
                set_status('running', 'Sign in below, including any 2FA/captcha if shown.', page.url)

                wait_started = time.monotonic()
                last_shot = 0.0
                while time.monotonic() < deadline:
                    if r.exists(GOOGLE_SIGNIN_STOP_KEY):
                        set_status('stopped', 'Cancelled')
                        return
                    if page.is_closed():
                        raise _BrowserSessionDied('browser page closed before sign-in completed')
                    now = time.monotonic()
                    if now - last_shot > 0.25:
                        last_shot = now
                        if _relay_input_and_screenshot(
                            page, r, waiting_since=wait_started,
                            stop_key=GOOGLE_SIGNIN_STOP_KEY, input_key=GOOGLE_SIGNIN_INPUT_KEY,
                            shot_key=GOOGLE_SIGNIN_SHOT_KEY, hint_key=GOOGLE_SIGNIN_HINT_KEY,
                        ):
                            set_status('stopped', 'Cancelled')
                            return
                    try:
                        cookies = context.cookies()
                        oauth_token = next((c['value'] for c in cookies if c['name'] == 'oauth_token'), None)
                    except Exception:  # noqa: BLE001
                        oauth_token = None
                    if oauth_token:
                        break
                    page.wait_for_timeout(200)
        except BaseException as exc:  # noqa: BLE001
            if _terminal_status_set['v']:
                logger.info('[youtubetv-google-signin] ignoring cleanup-time exception after terminal status was already set: %s', exc)
                return
            if r.exists(GOOGLE_SIGNIN_STOP_KEY):
                set_status('stopped', 'Cancelled')
                return
            logger.exception('[youtubetv-google-signin] browser session failed')
            set_status('error', f'Browser session failed: {exc}')
            return

        if not oauth_token:
            set_status('error', 'Timed out waiting for sign-in to complete.')
            return

        set_status('running', 'Verifying with Google…')
        from app.tve.google_master_token import exchange_oauth_token
        data = exchange_oauth_token(oauth_token)
        if not data:
            set_status('error', 'Google accepted the sign-in but the token exchange failed — see server logs.')
            return
        with flask_app.app_context():
            save_youtubetv_google_master_token(data)
        logger.info('[youtubetv-google-signin] captured a Google master_token for %s', data.get('email', '?'))
        set_status('success', f"Signed in as {data.get('email', '?')}.")
    finally:
        uninstall_browser_login_activity_log(_activity_handler)
        if not _ctx_popped['v']:
            _ctx.pop()
