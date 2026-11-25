import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from keep_session import HOME, check_owner, maintain, route


class SessionMaintenance(unittest.TestCase):
    def setUp(self):
        self.target = {"id": "page-1", "url": HOME["zoom"]}
        self.list_pages = Mock(return_value=[self.target])
        self.page = Mock()
        self.page.fault.return_value = False
        self.open_page = Mock(return_value=self.page)
        self.state = {}
        self.save = Mock()

    def tick(self, at=1000):
        return maintain("zoom", "example.zoom.us", self.state, at,
                        self.list_pages, self.open_page, self.save)

    def test_healthy_empty_surface_needs_no_action(self):
        self.page.evaluate.return_value = "ready"
        self.assertEqual(self.tick(), "ready")
        self.save.assert_not_called()
        self.page.call.assert_not_called()

    def test_crash_recovers_once_and_backs_off(self):
        self.page.fault.return_value = True
        self.assertEqual(self.tick(), "recovering")
        self.assertEqual(self.tick(1010), "cooldown")
        self.page.call.assert_called_once_with("Page.reload")
        self.save.assert_called_once_with("recovering")

    def test_identity_and_work_routes_are_untouched(self):
        for href, status in [
                ("https://login.microsoftonline.com/common/login", "sign_in_pending"),
                ("https://example.zoom.us/signin?state=example", "sign_in_pending"),
                (HOME["zoom"] + "?compose=1", "active_page"),
                ("https://app.zoom.us/wc/meeting/123", "active_page"),
                ("https://app.zoom.us.invalid/wc/home", "active_page"),
                ("https://@outlook.office.com/mail/", "active_page")]:
            self.target["url"] = href
            self.assertEqual(self.tick(), status)
        self.open_page.assert_not_called()

    def test_inactive_portal_and_multiple_tabs(self):
        self.target["url"] = "https://example.zoom.us/profile"
        self.assertEqual(self.tick(), "active_page")
        self.list_pages.return_value = [self.target, {"id": "b", "url": "about:blank"}]
        self.assertEqual(self.tick(), "multiple_tabs")
        self.open_page.assert_not_called()

    def test_confirmed_blank_resets_after_an_intervening_page(self):
        self.target["url"] = "about:blank"
        self.assertEqual(self.tick(), "empty_tab")
        self.target["url"] = "https://zoom.us/signin"
        self.assertEqual(self.tick(), "sign_in_pending")
        self.target["url"] = "about:blank"
        self.assertEqual(self.tick(), "empty_tab")
        self.page.evaluate.return_value = "recovering"
        self.assertEqual(self.tick(), "recovering")
        self.assertIn(HOME["zoom"], self.page.evaluate.call_args.args[0])

    def test_signed_out_opens_native_flow_and_returns_from_portal(self):
        self.page.evaluate.side_effect = ["signed_out", "recovering", "recovering"]
        self.assertEqual(self.tick(), "recovering")
        self.assertIn("https://example.zoom.us/signin", self.page.evaluate.call_args.args[0])
        self.target["url"] = "https://example.zoom.us/signin"
        self.assertEqual(self.tick(1001), "sign_in_pending")
        self.target["url"] = "https://example.zoom.us/profile"
        self.assertEqual(self.tick(1002), "recovering")
        self.assertEqual(self.tick(1003), "active_page")

    def test_unverified_and_in_use_pages_are_preserved(self):
        for value in ["ui_unverified", "input_in_use"]:
            self.page.evaluate.return_value = value
            self.assertEqual(self.tick(), value)
        self.save.assert_not_called()

    def test_page_change_before_reload_aborts(self):
        self.page.fault.return_value = True
        self.list_pages.side_effect = [[self.target], [{"id": "b", "url": self.target["url"]}]]
        self.assertEqual(self.tick(), "target_changed")
        self.page.call.assert_not_called()

    def test_outlook_root_and_active_messages(self):
        self.assertEqual(route("outlook", HOME["outlook"], None), "home")
        self.assertEqual(route("outlook", "https://outlook.cloud.microsoft/mail", None), "home")
        self.assertEqual(route("outlook", HOME["outlook"] + "id/example", None), "work")

    def test_outlook_inbox_is_readable_without_recovery_navigation(self):
        self.target["url"] = HOME["outlook"] + "0/inbox"
        for observed, expected in [("ready", "ready"), ("signed_out", "active_page")]:
            self.page.evaluate.return_value = observed
            result = maintain("outlook", None, self.state, 1000, self.list_pages, self.open_page, self.save)
            self.assertEqual(result, expected)
        self.page.call.assert_not_called()
        self.save.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is required for the offline DOM fixture")
    def test_outlook_signed_out_only_follows_visible_authorize_link(self):
        self.target["url"] = HOME["outlook"]
        fixture = """
            const {expression, home, urls} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            let assigned = null;
            const location = { href: home, assign: href => { assigned = href; } };
            const links = urls.map(([href, visible]) => ({
                href, textContent: 'Sign in', getAttribute: () => null,
                getClientRects: () => visible ? [1] : []
            }));
            const document = { querySelectorAll: selector => selector.startsWith('a[href]') ? links : [] };
            const status = eval(expression);
            process.stdout.write(JSON.stringify({status, assigned}));
        """
        authorize = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?state=demo&prompt=none"
        unsuitable = [
            [authorize, False],
            [authorize.replace(".com/", ".com.invalid/"), True],
            [authorize.replace("//login", "//example@login"), True],
            [authorize.replace("https:", "http:"), True],
            [authorize.replace("/authorize", "/logout"), True],
        ]
        for urls, expected in [(unsuitable, "sign_in_pending"), (unsuitable + [[authorize, True]], "recovering")]:
            with self.subTest(expected=expected):
                assigned = []

                def evaluate(expression):
                    payload = dict(expression=expression, home=HOME["outlook"], urls=urls)
                    result = subprocess.run(["node", "-e", fixture], input=json.dumps(payload),
                                            text=True, capture_output=True, check=True)
                    output = json.loads(result.stdout)
                    if output["assigned"]:
                        assigned.append(output["assigned"])
                    return output["status"]

                self.page.evaluate.side_effect = evaluate
                result = maintain("outlook", None, self.state, 1000, self.list_pages, self.open_page, self.save)
                self.assertEqual(result, expected)
                self.assertEqual(assigned, [authorize] if expected == "recovering" else [])
                self.assertNotIn(authorize, json.dumps(self.state))

    @patch("keep_session.subprocess.check_output")
    def test_listener_must_be_loopback_only(self, run):
        run.side_effect = ["pid = 1234\n", "n127.0.0.1:19321\nn*:19321\n"]
        with self.assertRaises(ValueError):
            check_owner("com.example.browser", 19321)


if __name__ == "__main__":
    unittest.main()
