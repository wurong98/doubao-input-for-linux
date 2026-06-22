"""WebKitGTK-based login window for doubao.com.

Mirrors WebViewManager.swift (macOS).

- Loads doubao.com/chat in a WebKit2 4.0 WebView (GTK3)
- Injects JS to detect login via /alice/profile/self API interception
- Extracts cookies + localStorage params after login
- Destroys WebView after params are extracted to free memory

GTK3/WebKit2-4.0 port note: The original file targeted GTK4 +
WebKitGTK 6 with a fallback to WebKit2 4.1. On Ubuntu 20.04 we only
have WebKit2 4.0, whose JS API uses `run_javascript` / `run_javascript_finish`
returning `WebKitJavascriptResult` (not `evaluate_javascript_finish`).
Cookies live on the WebView's context, not a NetworkSession.
"""

from __future__ import annotations

import json
import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from doubao_input.doubao.app_state import AppState, LoginStatus
from doubao_input.doubao.config import LOGIN_URL, WEBVIEW_USER_AGENT
from doubao_input.doubao.params_store import ASRParams

logger = logging.getLogger(__name__)

# WebKit2 4.0 (the only WebKit version on Ubuntu 20.04). We deliberately
# don't probe for WebKit 6 / WebKit2 4.1 here — those require GTK 4 and
# this fork targets GTK 3.
_HAS_WEBKIT = False
try:
    gi.require_version("WebKit2", "4.0")
    from gi.repository import WebKit2 as WebKit

    _HAS_WEBKIT = True
except (ImportError, ValueError) as exc:
    logger.warning("WebKit2 4.0 not available: %s", exc)


class LoginWindow:
    """WebKit2-4.0-based login window for doubao.com (GTK3)."""

    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state
        self._window = None  # type: ignore[assignment]
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

        # WebKitGTK settings
        settings = WebKit.Settings()
        settings.set_property("enable-developer-extras", True)
        settings.set_property("user-agent", WEBVIEW_USER_AGENT)

        # User content manager for JS injection
        user_content = WebKit.UserContentManager()

        # Inject login detection JS at document start
        ws_js = self._load_js_resource("inject-websocket.js")
        if ws_js:
            adapted_js = ws_js.replace(
                "window.webkit.messageHandlers.asrHandler.postMessage",
                "window.webkit.messageHandlers.asr_handler.postMessage",
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

        # Register message handler. WebKit2 4.0 only takes the handler name.
        user_content.register_script_message_handler("asr_handler")
        user_content.connect(
            "script-message-received::asr_handler", self._on_script_message
        )

        # Create WebView with the user content manager (construct-only prop).
        self._webview = WebKit.WebView(user_content_manager=user_content)
        self._webview.set_settings(settings)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.connect("decide-policy", self._on_decide_policy)

        # GTK3 window
        self._window = Gtk.Window()
        self._window.set_title("Doubao Murmur - 登录")
        self._window.set_default_size(1280, 800)
        self._window.set_position(Gtk.WindowPosition.CENTER)
        self._window.add(self._webview)
        # GTK3 uses delete-event (returning True hides instead of destroys).
        self._window.connect("delete-event", self._on_delete_event)

    def load(self) -> None:
        """Create webview (if needed) and load doubao.com."""
        self._setup()
        if self._webview:
            self._webview.load_uri(LOGIN_URL)

    def show(self) -> None:
        if not self._webview:
            self.load()
        if self._window:
            self._window.show_all()
            self._window.present()

    def hide(self) -> None:
        if self._window:
            self._window.hide()

    def destroy(self) -> None:
        """Destroy WebView to free memory (mirrors destroyWebView())."""
        if self._webview:
            try:
                self._webview.stop_loading()
            except Exception:
                pass
            self._webview = None
        if self._window:
            self._window.destroy()
            self._window = None
        logger.info("WebView destroyed")

    # ---- Param extraction ----

    def extract_params_async(self, callback) -> None:
        """Extract cookies + localStorage params from WebView.

        callback(params: ASRParams | None) called on GTK main thread.
        """
        if not self._webview:
            callback(None)
            return

        cookie_manager = self._get_cookie_manager()

        def on_cookies_finish(source, result, *_args):
            try:
                cookies = cookie_manager.get_cookies_finish(result)
            except Exception as exc:
                logger.error("Cookie fetch failed: %s", exc)
                GLib.idle_add(callback, None)
                return

            doubao_cookies = {}
            for cookie in cookies:
                domain = cookie.get_domain()
                if domain and "doubao.com" in domain:
                    doubao_cookies[cookie.get_name()] = cookie.get_value()

            if not doubao_cookies:
                logger.warning("No doubao.com cookies found")
                GLib.idle_add(callback, None)
                return

            self._extract_local_storage(doubao_cookies, callback)

        # WebKit2 4.0: get_cookies(uri, cancellable, callback, user_data)
        cookie_manager.get_cookies(LOGIN_URL, None, on_cookies_finish, None)

    def _extract_local_storage(self, cookies: dict, callback) -> None:
        """Extract device_id and web_id from localStorage."""
        js_code = """
        JSON.stringify({
            device_id_raw: localStorage.getItem('samantha_web_web_id'),
            tea_cache_raw: localStorage.getItem('__tea_cache_tokens_497858')
        })
        """

        def on_js_finish(source, result, *_args):
            try:
                # WebKit2 4.0: returns WebKitJavascriptResult
                js_result = self._webview.run_javascript_finish(result)
                json_str = self._js_result_to_string(js_result)
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

        # WebKit2 4.0: run_javascript(script, cancellable, callback, user_data)
        self._webview.run_javascript(js_code, None, on_js_finish, None)

    def _get_cookie_manager(self):
        """WebKit2 4.0: cookies live on the WebContext."""
        return self._webview.get_context().get_cookie_manager()

    # ---- Login detection ----

    def _on_script_message(self, manager, js_result) -> None:
        """Handle messages from injected JS (login detection)."""
        try:
            json_str = self._js_result_to_string(js_result)
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
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            try:
                nav_action = decision.get_navigation_action()
                uri = nav_action.get_request().get_uri()
                if uri and "from_login=1" in uri:
                    self._notify_login_status("loggedIn", None)
            except Exception:
                pass
        return False  # default handling

    def _check_login_fallback(self) -> bool:
        if not self._webview:
            return GLib.SOURCE_REMOVE
        js = "window.__doubaoMurmur && window.__doubaoMurmur.isLoginButtonPresent()"

        def on_result(source, result, *_args):
            try:
                if not self._webview:
                    return
                js_result = self._webview.run_javascript_finish(result)
                value = js_result.get_js_value()
                if value.to_boolean():
                    if self.app_state.login_status == LoginStatus.CHECKING:
                        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
            except Exception:
                pass

        self._webview.run_javascript(js, None, on_result, None)
        return GLib.SOURCE_REMOVE

    def _on_delete_event(self, window, _event) -> bool:
        window.hide()
        return True  # don't actually destroy

    def logout(self) -> None:
        """Clear saved params and reload."""
        from doubao_input.doubao.params_store import ParamsStore

        ParamsStore.clear()
        self.app_state.login_status = LoginStatus.NOT_LOGGED_IN
        if self._webview:
            self._clear_website_data(self.load)

    def _clear_website_data(self, callback=None) -> None:
        """WebKit2 4.0: WebsiteDataManager hangs off the WebContext."""
        if not self._webview:
            if callback:
                callback()
            return
        try:
            data_manager = self._webview.get_website_data_manager()
        except Exception:
            try:
                data_manager = self._webview.get_context().get_website_data_manager()
            except Exception:
                if callback:
                    callback()
                return

        clear = getattr(data_manager, "clear", None)
        if clear is None:
            if callback:
                callback()
            return

        types = getattr(WebKit.WebsiteDataTypes, "ALL", None)
        if types is None:
            types = 0xFFFFFFFF

        def on_done(source, result, *_args):
            finish = getattr(data_manager, "clear_finish", None)
            if finish is not None and result is not None:
                try:
                    finish(result)
                except Exception as e:
                    logger.warning("WebKit clear_finish failed: %s", e)
            if callback:
                callback()

        try:
            clear(types, 0, None, on_done, None)
        except TypeError:
            clear(types, 0, None, on_done)

    def _notify_login_status(self, status: str, nickname) -> None:
        if self._on_login_status_change:
            self._on_login_status_change(status, nickname)

    # ---- helpers ----

    @staticmethod
    def _js_result_to_string(js_result) -> str:
        """Convert a WebKit2 4.0 WebKitJavascriptResult to a JSON string.

        Also handles the case where the script message handler passes us
        a raw JSC value, or a dict already (unit-test convenience).
        """
        if isinstance(js_result, str):
            return js_result
        if isinstance(js_result, dict):
            return json.dumps(js_result)

        # WebKit2 4.0 WebKitJavascriptResult.get_js_value() -> JSC.Value
        value = js_result
        get_js_value = getattr(js_result, "get_js_value", None)
        if get_js_value is not None:
            value = get_js_value()

        # JSC.Value disambiguation:
        # - JS 里写 `JSON.stringify({...})` 返回的是一个 JS 字符串. 对
        #   JSC string value 调 `to_json()` 会再做一次 JSON 编码 (变成
        #   `"\"{...}\""`), 再 `json.loads` 就只剥一层引号, 得到的
        #   还是 string, 后续 `.get` 就崩.
        # - 正确做法: 如果 JSC.Value 是字符串, 用 `to_string()` 拿原文.
        is_string = getattr(value, "is_string", None)
        if callable(is_string):
            try:
                if is_string():
                    return value.to_string()
            except Exception:
                pass
        # 非字符串 (对象/数组/数字/布尔/null) 才走 to_json.
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
        raise TypeError(f"Unsupported JS value: {type(js_result)!r}")

    @staticmethod
    def _load_js_resource(name: str):
        """Load JS file from resources directory (../login/resources)."""
        try:
            from pathlib import Path
            here = Path(__file__).resolve().parent
            res = here.parent / "login" / "resources" / name
            return res.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Cannot load JS resource %s: %s", name, e)
            return None
