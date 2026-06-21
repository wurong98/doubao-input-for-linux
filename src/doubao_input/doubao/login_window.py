"""WebKitGTK-based login window for doubao.com.

Mirrors WebViewManager.swift.

- Loads doubao.com/chat in a WebKitGTK WebView
- Injects JS to detect login via /alice/profile/self API interception
- Extracts cookies + localStorage params after login
- Destroys WebView after params are extracted to free memory
"""

from __future__ import annotations

import importlib.resources  # noqa: F401  # kept for forward compat; resources now loaded via Path
import json
import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from doubao_input.doubao.app_state import AppState, LoginStatus
from doubao_input.doubao.config import LOGIN_URL, WEBVIEW_USER_AGENT
from doubao_input.doubao.params_store import ASRParams

logger = logging.getLogger(__name__)

# Try WebKitGTK
_HAS_WEBKIT = False
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit

    _HAS_WEBKIT = True
except (ImportError, ValueError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit

        _HAS_WEBKIT = True
    except (ImportError, ValueError):
        pass


class LoginWindow:
    """WebKitGTK-based login window for doubao.com."""

    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state
        self._window: Gtk.Window | None = None
        self._webview = None
        self._on_login_status_change = None  # (status, nickname) -> None

    @property
    def is_active(self) -> bool:
        return self._webview is not None

    @staticmethod
    def is_available() -> bool:
        return _HAS_WEBKIT

    def _setup(self) -> None:
        if self._webview or not _HAS_WEBKIT:
            return

        # WebKitGTK configuration
        settings = WebKit.Settings()
        settings.set_property("enable-developer-extras", True)
        settings.set_property("user-agent", WEBVIEW_USER_AGENT)

        # User content manager for JS injection
        user_content = WebKit.UserContentManager()

        # Inject login detection JS at document start
        ws_js = self._load_js_resource("inject-websocket.js")
        if ws_js:
            # WebKitGTK uses the same messageHandlers API as WKWebView
            # Normalize handler name
            adapted_js = ws_js.replace(
                "window.webkit.messageHandlers.asrHandler.postMessage",
                "window.webkit.messageHandlers.asr_handler.postMessage"
            )
            script = WebKit.UserScript.new(
                adapted_js,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
                None,
                None,
            )
            user_content.add_script(script)

        # Inject DOM helpers at document end
        dom_js = self._load_js_resource("inject-dom.js")
        if dom_js:
            script = WebKit.UserScript.new(
                dom_js,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.END,
                None,
                None,
            )
            user_content.add_script(script)

        # Register message handler. WebKitGTK 6 accepts a content-world
        # argument; older WebKit2GTK only accepts the handler name.
        try:
            user_content.register_script_message_handler("asr_handler", None)
        except TypeError:
            user_content.register_script_message_handler("asr_handler")
        user_content.connect(
            "script-message-received::asr_handler", self._on_script_message
        )

        # Create WebView. WebKitGTK 6 has no
        # new_with_user_content_manager(); the construct-only property
        # works on both WebKitGTK 6 and WebKit2GTK 4.x.
        self._webview = WebKit.WebView(user_content_manager=user_content)
        self._webview.set_settings(settings)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.connect("decide-policy", self._on_decide_policy)

        # Create window
        self._window = Gtk.Window()
        self._window.set_title("Doubao Murmur - 登录")
        self._window.set_default_size(1280, 800)
        self._window.set_child(self._webview)
        self._window.connect("close-request", self._on_close_request)

    def load(self) -> None:
        """Create webview (if needed) and load doubao.com."""
        self._setup()
        if self._webview:
            self._webview.load_uri(LOGIN_URL)

    def show(self) -> None:
        if not self._webview:
            self.load()
        if self._window:
            self._window.present()

    def hide(self) -> None:
        if self._window:
            self._window.set_visible(False)

    def destroy(self) -> None:
        """Destroy WebView to free memory (mirrors destroyWebView())."""
        if self._webview:
            self._webview.stop_loading()
            self._webview = None
        if self._window:
            self._window.destroy()
            self._window = None
        logger.info("WebView destroyed")

    def extract_params_async(self, callback) -> None:
        """Extract cookies + localStorage params from WebView.

        callback(params: ASRParams | None) called on GTK main thread.
        """
        if not self._webview:
            callback(None)
            return

        # Step 1: Get cookies via WebKit.CookieManager
        cookie_manager = self._get_cookie_manager()

        def on_cookies_finish(source, result=None, *_args):
            if result is None:
                result = source
            try:
                cookies = cookie_manager.get_cookies_finish(result)
            except Exception:
                GLib.idle_add(callback, None)
                return

            doubao_cookies: dict[str, str] = {}
            for cookie in cookies:
                domain = cookie.get_domain()
                if "doubao.com" in domain:
                    doubao_cookies[cookie.get_name()] = cookie.get_value()

            if not doubao_cookies:
                logger.warning("No doubao.com cookies found")
                GLib.idle_add(callback, None)
                return

            # Step 2: Extract localStorage values via JS
            self._extract_local_storage(doubao_cookies, callback)

        cookie_manager.get_cookies(
            LOGIN_URL,
            None,  # cancellable
            on_cookies_finish,
            None,  # user_data
        )

    def _extract_local_storage(self, cookies: dict, callback) -> None:
        """Extract device_id and web_id from localStorage."""
        js_code = """
        JSON.stringify({
            device_id_raw: localStorage.getItem('samantha_web_web_id'),
            tea_cache_raw: localStorage.getItem('__tea_cache_tokens_497858')
        })
        """

        def on_js_finish(source, result=None, *_args):
            if result is None:
                result = source
            try:
                js_value = self._webview.evaluate_javascript_finish(result)
                json_str = js_value.to_string()
                data = json.loads(json_str)

                device_id = ""
                web_id = ""

                if data.get("device_id_raw"):
                    parsed = json.loads(data["device_id_raw"])
                    device_id = parsed.get("web_id", "")

                if data.get("tea_cache_raw"):
                    parsed = json.loads(data["tea_cache_raw"])
                    web_id = parsed.get("web_id", "")

                if device_id and web_id:
                    params = ASRParams(
                        cookies=cookies,
                        device_id=device_id,
                        web_id=web_id,
                    )
                    logger.info(
                        "Params extracted: %d cookies, device=%s, web=%s",
                        len(cookies),
                        device_id[:10],
                        web_id[:10],
                    )
                    GLib.idle_add(callback, params)
                else:
                    logger.warning(
                        "Missing localStorage params: device=%s, web=%s",
                        device_id,
                        web_id,
                    )
                    GLib.idle_add(callback, None)
            except Exception as e:
                logger.error("JS evaluation failed: %s", e)
                GLib.idle_add(callback, None)

        self._webview.evaluate_javascript(
            js_code,
            -1,
            None,  # world_name
            None,  # source_uri
            None,  # cancellable
            on_js_finish,
            None,  # user_data
        )

    def _get_cookie_manager(self):
        """WebKitGTK 6 moved cookies to NetworkSession; 4.x uses the
        website data manager."""
        if hasattr(self._webview, "get_network_session"):
            return self._webview.get_network_session().get_cookie_manager()
        return self._webview.get_website_data_manager().get_cookie_manager()

    def _get_website_data_manager(self):
        if hasattr(self._webview, "get_network_session"):
            return self._webview.get_network_session().get_website_data_manager()
        return self._webview.get_website_data_manager()

    def _on_script_message(self, manager, js_result) -> None:
        """Handle messages from injected JS (login detection)."""
        try:
            json_str = self._json_string_from_js_value(js_result)
            data = json.loads(json_str)
        except Exception:
            return

        msg_type = data.get("type")
        if msg_type == "login":
            status = data.get("status", "unknown")
            nickname = data.get("nickname")
            self._notify_login_status(status, nickname)

    def _on_load_changed(self, webview, event) -> None:
        if event == WebKit.LoadEvent.FINISHED:
            GLib.timeout_add(2000, self._check_login_fallback)

    def _on_decide_policy(self, webview, decision, decision_type) -> bool:
        """Detect login redirect via URL parameter."""
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
            if "from_login=1" in uri:
                self._notify_login_status("loggedIn", None)
        return False  # Allow default handling

    def _check_login_fallback(self) -> bool:
        """Fallback: check if login button is present in DOM."""
        if not self._webview:
            return GLib.SOURCE_REMOVE
        js = "window.__doubaoMurmur && window.__doubaoMurmur.isLoginButtonPresent()"
        self._webview.evaluate_javascript(
            js,
            -1,
            None,
            None,
            None,
            self._on_login_check_result,
            None,
        )
        return GLib.SOURCE_REMOVE

    def _on_login_check_result(self, source, result=None, *_args) -> None:
        if result is None:
            result = source
        try:
            if self._webview:
                val = self._webview.evaluate_javascript_finish(result)
                if val.to_boolean():
                    if self.app_state.login_status == LoginStatus.CHECKING:
                        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        except Exception:
            pass

    def _on_close_request(self, window) -> bool:
        """Hide instead of destroy when user closes the window."""
        window.set_visible(False)
        return True  # Prevent default destroy

    def logout(self) -> None:
        """Clear saved params and reload."""
        from doubao_input.doubao.params_store import ParamsStore

        ParamsStore.clear()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self._webview:
            self._clear_website_data(self.load)

    def _clear_website_data(self, callback=None) -> None:
        """Clear WebKitGTK website data for this app before reloading login."""
        if not self._webview:
            if callback:
                callback()
            return

        data_manager = self._get_website_data_manager()
        clear = getattr(data_manager, "clear", None)
        if clear is None:
            if callback:
                callback()
            return

        types = getattr(WebKit.WebsiteDataTypes, "ALL", None)
        if types is None:
            types = 0xFFFFFFFF

        def on_clear_finished(source, result=None, *_args):
            finish = getattr(data_manager, "clear_finish", None)
            if finish is not None and result is not None:
                try:
                    finish(result)
                except Exception as e:
                    logger.warning("Failed to finish WebKit data clear: %s", e)
            if callback:
                callback()

        try:
            clear(types, 0, None, on_clear_finished, None)
        except TypeError:
            clear(types, 0, None, on_clear_finished)

    def _notify_login_status(self, status: str, nickname: str | None) -> None:
        if self._on_login_status_change:
            self._on_login_status_change(status, nickname)

    @staticmethod
    def _json_string_from_js_value(value) -> str:
        """Convert WebKitGTK/JSC script message values to a JSON string."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return json.dumps(value)
        if hasattr(value, "get_js_value"):
            value = value.get_js_value()
        to_json = getattr(value, "to_json", None)
        if to_json is not None:
            try:
                result = to_json(0)
            except TypeError:
                result = to_json()
            if result:
                return result
        to_string = getattr(value, "to_string", None)
        if to_string is not None:
            return to_string()
        raise TypeError(f"Unsupported JS value: {type(value)!r}")

    @staticmethod
    def _load_js_resource(name: str) -> str | None:
        """Load JS file from resources directory (../login/resources)."""
        try:
            from pathlib import Path
            here = Path(__file__).resolve().parent
            res = here.parent / "login" / "resources" / name
            return res.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Cannot load JS resource %s: %s", name, e)
            return None
