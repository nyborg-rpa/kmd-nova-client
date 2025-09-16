import asyncio
import json
import os
import sys
from asyncio import WindowsProactorEventLoopPolicy
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, TypedDict

import pandas as pd
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from tqdm import tqdm


class KMDNovaSearchResult(TypedDict):
    ItemsCount: int
    HasMoreItems: bool
    Items: list[dict]


class KMDNovaClient:

    BASE_URL = "https://cap-awswlbs-wm3q2021.kmd.dk/KMDNovaESDH"
    MAX_NUM_SEARCH_RESULTS = 100

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

    def search(
        self,
        *,
        endpoint: Literal["case", "document"],
        query: dict[str],
        num_results: int | Literal["all"] = 100,
        chunk_period: pd.Timedelta | str | None = None,
    ) -> list[dict]:
        """
        Search for cases or documents in KMD Nova.
        """

        # the results we return
        items: list[dict] = []

        # if a chunk period is given, do the search by chunking the query date range
        if chunk_period:

            chunk_period = pd.Timedelta(chunk_period)
            date_ranges = pd.date_range(
                start=pd.Timestamp(query.get("UpdateDatePeriod", {}).get("ToDate", "now")),
                end=pd.Timestamp(query.get("UpdateDatePeriod", {}).get("FromDate", "2015-01-01")),
                freq=-chunk_period,
            )

            tqdm.write(
                f"chunking {len(date_ranges)} periods of {chunk_period} from {date_ranges[-1]:%Y-%m-%d} to {date_ranges[0]:%Y-%m-%d}..."
            )
            for date in tqdm(date_ranges):

                from_date = date - chunk_period
                to_date = date.replace(hour=23, minute=59, second=59)

                tqdm.write(f"chunking from {from_date}...")

                chunked_query = query | {
                    "UpdateDatePeriod": {
                        "StandardDatePeriod": "FromToDate",
                        "FromDate": from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "ToDate": to_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    }
                }

                items += self.search(
                    endpoint=endpoint,
                    query=chunked_query,
                    num_results=num_results,
                    chunk_period=None,
                )

        # otherwise, just fetch the data as specified (no chunking)
        else:

            tqdm.write(f"searching KMD Nova {endpoint=}...")

            url = f"{self.BASE_URL}/api/ServiceRelayer/kmdnova/v1/{endpoint}/advancedsearch"
            referer = {"case": "sager", "document": "dokumenter"}[endpoint]
            headers = dict(self.session.headers) | {
                "Referer": f"{self.BASE_URL}/udvidet-soegning/{referer}",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "da",
                "Connection": "keep-alive",
                "Content-Type": "application/json;charset=UTF-8",
            }

            resp = self.session.post(
                url=url,
                headers=headers,
                data=json.dumps(query),
            )

            resp.raise_for_status()
            data: KMDNovaSearchResult = resp.json()
            items += data["Items"]

            # do we need to fetch the remaining data in chunks?
            num_results = data["ItemsCount"] if num_results == "all" else num_results
            if num_results > self.MAX_NUM_SEARCH_RESULTS and data["HasMoreItems"]:
                for offset in range(100, num_results, self.MAX_NUM_SEARCH_RESULTS):
                    new_query = query | {"NumberOfDocumentsAlreadySent": offset}
                    items += self.search(
                        endpoint=endpoint,
                        query=new_query,
                        num_results=self.MAX_NUM_SEARCH_RESULTS,
                    )

        # remove duplicates (keep order)
        items = list({item["Id"]: item for item in items}.values())

        return items

