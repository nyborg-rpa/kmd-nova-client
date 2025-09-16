import asyncio
import os
import sys
from asyncio import WindowsProactorEventLoopPolicy
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from tqdm import tqdm


class KMDNovaClient:

    BASE_URL = "https://cap-awswlbs-wm3q2021.kmd.dk/KMDNovaESDH"

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
    ):

        tqdm.write("initializing KMD Nova client...")

        if not username or not password:
            load_dotenv(override=True, verbose=True)
            username = username or os.getenv("KMD_NOVA_USERNAME")
            password = password or os.getenv("KMD_NOVA_PASSWORD")

        self.session = self._create_session(
            username=username,
            password=password,
        )

    @staticmethod
    def _create_session(
        *,
        username: str,
        password: str,
    ) -> requests.Session:

        # try to start Playwright synchronously
        # this will fail when running in a Jupyter notebook or other async context
        # so we need to run it in a separate thread
        try:
            p = sync_playwright().start()

        except Exception:

            tqdm.write("failed to start sync_playwright(); retrying in a separate thread...")

            # if we are on Windows, we need to set the event loop policy to ProactorEventLoopPolicy
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(WindowsProactorEventLoopPolicy())

            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(KMDNovaClient._create_session, username=username, password=password).result()

        tqdm.write(f"creating KMD Nova session for {username=}...")

        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        try:

            # log in
            page.goto(url=KMDNovaClient.BASE_URL, wait_until="networkidle")
            page.fill("input[name='UserInfo.Username']", username)
            page.fill("input[name='UserInfo.Password']", password)
            page.click("#logonBtn")
            page.wait_for_load_state("networkidle")

            # TODO: check if we are logged in
            ...
            # while True:
            #     page.wait_for_timeout(1000)

            # create a session with cookies and headers from the browser
            # and extract request verification token from the page
            session = requests.Session()
            cookies = ctx.cookies()
            user_agent = page.evaluate("() => navigator.userAgent")
            req_ver_token = page.query_selector("input[ncg-request-verification-token]").get_attribute(
                "ncg-request-verification-token"
            )

            session.headers.update(
                {
                    "User-Agent": user_agent,
                    "RequestVerificationToken": req_ver_token,
                }
            )

            for c in cookies:
                session.cookies.set(
                    name=c["name"],
                    value=c["value"],
                    domain=c.get("domain"),
                    path=c.get("path"),
                )

        except Exception as e:
            raise RuntimeError(f"Failed to create KMD Nova session: {e}") from e

        finally:
            browser.close()
            p.stop()

        return session

    def _check_session(self, session: requests.Session) -> None:
        """
        Check if the session is still valid.
        If not, raise an exception.
        """
        resp = session.get(f"{self.BASE_URL}/forside")
        resp.raise_for_status()
