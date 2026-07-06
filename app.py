import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs
from fastapi.responses import Response
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from camoufox.async_api import AsyncCamoufox

# ───────────────────────────── Config ─────────────────────────────

CAMOUFOX_HEADLESS = os.getenv("CAMOUFOX_HEADLESS", "virtual")  # virtual | true | false
CAMOUFOX_OS = os.getenv("CAMOUFOX_OS", "windows")
CAMOUFOX_LOCALE = os.getenv("CAMOUFOX_LOCALE", "en-US")
CAMOUFOX_HUMANIZE = os.getenv("CAMOUFOX_HUMANIZE", "true").lower() == "true"
CAMOUFOX_PROFILE_DIR = os.getenv("CAMOUFOX_PROFILE_DIR", "/app/camoufox-profile")

if CAMOUFOX_HEADLESS == "true":
    HEADLESS_VAL = True
elif CAMOUFOX_HEADLESS == "false":
    HEADLESS_VAL = False
else:
    HEADLESS_VAL = "virtual"


def now_iso():
    return datetime.now().isoformat()


# ───────────────────────────── Models ─────────────────────────────

class ProfilePostsRequest(BaseModel):
    url: str


class ProfilePostsNextRequest(BaseModel):
    after: str
    username: Optional[str] = None


class CommentRequest(BaseModel):
    pk: str
    code: Optional[str] = None
    max_loops: int = 50


class ProfileRequest(BaseModel):
    url: Optional[str] = None
    id: Optional[str] = None
    username: Optional[str] = None


class InjectCookieRequest(BaseModel):
    cookie_string: str
    domain: str
    path: str = "/"
    secure: bool = True

class InstagramRequest(BaseModel):
    username: str
    count: Optional[int] = None
    date: Optional[str] = None

# ───────────────────────────── Query Cache ─────────────────────────────

QUERY_CACHE: dict[str, dict] = {}


def _harvest_request(req):
    try:
        if req.method != "POST":
            return
        if "/graphql/query" not in req.url and "/api/graphql" not in req.url:
            return

        friendly = req.headers.get("x-fb-friendly-name")
        root_field = req.headers.get("x-root-field-name")

        post_data = req.post_data or ""
        parsed = parse_qs(post_data)
        doc_id = (parsed.get("doc_id") or [None])[0]
        variables_raw = (parsed.get("variables") or [None])[0]

        if not friendly:
            friendly = (parsed.get("fb_api_req_friendly_name") or [None])[0]

        endpoint = "/api/graphql" if "/api/graphql" in req.url else "/graphql/query"

        if friendly and doc_id:
            QUERY_CACHE[friendly] = {
                "doc_id": doc_id,
                "root_field": root_field or "",
                "endpoint": endpoint,
                "variables_raw": variables_raw,
            }
            print(f"[harvest] {friendly} → doc_id={doc_id} ep={endpoint} root_field={root_field}")
    except Exception as e:
        print(f"[harvest] error: {e}")


# ───────────────────────────── Lifespan ─────────────────────────────

async def _launch_camoufox():
    cam = AsyncCamoufox(
        headless=HEADLESS_VAL,
        os=CAMOUFOX_OS,
        locale=CAMOUFOX_LOCALE,
        humanize=CAMOUFOX_HUMANIZE,
        persistent_context=True,
        user_data_dir=CAMOUFOX_PROFILE_DIR,
        geoip=True,
    )
    browser = await cam.__aenter__()
    return cam, browser


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(CAMOUFOX_PROFILE_DIR, exist_ok=True)
    app.state.cam = None
    app.state.browser = None
    app.state.page = None
    app.state.lock = asyncio.Lock()
    try:
        yield
    finally:
        try:
            page = getattr(app.state, "page", None)
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            if app.state.cam:
                await app.state.cam.__aexit__(None, None, None)
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


# ───────────────────────────── Browser ─────────────────────────────

async def connect_browser():
    page = getattr(app.state, "page", None)
    if page is not None:
        try:
            if not page.is_closed():
                await page.evaluate("1")
                return page
        except Exception:
            pass

    # 브라우저가 죽었거나 처음 시작
    if app.state.cam is None or app.state.browser is None:
        app.state.cam, app.state.browser = await _launch_camoufox()

    # persistent_context 모드에서는 browser 자체가 context
    context = app.state.browser

    # 기존 페이지 재사용 or 새로 생성
    if context.pages:
        page = context.pages[0]
    else:
        page = await context.new_page()

    page.on("request", _harvest_request)

    app.state.page = page
    return page


async def ensure_page():
    try:
        return await connect_browser()
    except Exception as e:
        print(f"[ensure_page] reconnect after error: {e}")
        # 브라우저 죽었으면 정리하고 재시작
        try:
            if app.state.cam:
                await app.state.cam.__aexit__(None, None, None)
        except Exception:
            pass
        app.state.cam = None
        app.state.browser = None
        app.state.page = None
        return await connect_browser()


# ───────────────────────────── Username 추출 ─────────────────────────────

def extract_username(url: str) -> str:
    path = url.split("instagram.com/", 1)[-1]
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    return path.split("/", 1)[0]


# ───────────────────────────── Query 선택 ─────────────────────────────

def find_query(kind: str) -> Optional[dict]:
    for name, meta in QUERY_CACHE.items():
        if kind == "first" and "ProfilePosts" in name and "TabContent" not in name:
            return {"friendly_name": name, **meta}
        if kind == "next" and "ProfilePostsTabContent" in name:
            return {"friendly_name": name, **meta}
        if kind == "profile" and "ProfilePageContent" in name:
            return {"friendly_name": name, **meta}
    return None


async def wait_for_query(kind: str, timeout: float = 10.0) -> dict:
    interval = 0.2
    waited = 0.0
    while waited < timeout:
        found = find_query(kind)
        if found:
            return found
        await asyncio.sleep(interval)
        waited += interval
    raise RuntimeError(
        f"query meta not captured for kind={kind} "
        f"(cache keys: {list(QUERY_CACHE.keys())})"
    )


# ───────────────────────────── GraphQL Fetch ─────────────────────────────

GRAPHQL_JS = """
async ({ doc_id, friendly_name, root_field, variables, endpoint }) => {
    const html = document.documentElement.innerHTML;

    const pickFirst = (...regs) => {
        for (const r of regs) {
            const m = html.match(r);
            if (m) return m[1];
        }
        return null;
    };

    const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
    const fb_dtsg   = pickFirst(/"DTSGInitialData",\\[\\],\\{"token":"([^"]+)"/);
    const lsd       = pickFirst(/"LSD",\\[\\],\\{"token":"([^"]+)"/);
    const appId     = pickFirst(/"X-IG-App-ID":"(\\d+)"/, /"APP_ID":"(\\d+)"/);
    const av        = pickFirst(/"actorID":"(\\d+)"/, /"viewerId":"(\\d+)"/) || "0";

    if (!fb_dtsg || !lsd) {
        throw new Error("token extraction failed: fb_dtsg=" + !!fb_dtsg + " lsd=" + !!lsd);
    }
    if (!appId) {
        throw new Error("x-ig-app-id resolve failed");
    }

    let sum = 0;
    for (let i = 0; i < fb_dtsg.length; i++) sum += fb_dtsg.charCodeAt(i);
    const jazoest = "2" + sum;

    const body = new URLSearchParams({
        av,
        __a: "1",
        __req: "1",
        __ccg: "EXCELLENT",
        dpr: String(window.devicePixelRatio || 1),
        fb_dtsg,
        jazoest,
        lsd,
        fb_api_caller_class: "RelayModern",
        fb_api_req_friendly_name: friendly_name,
        variables: JSON.stringify(variables),
        server_timestamps: "true",
        doc_id
    });

    const headers = {
        "accept": "*/*",
        "content-type": "application/x-www-form-urlencoded",
        "x-csrftoken": csrftoken,
        "x-fb-lsd": lsd,
        "x-fb-friendly-name": friendly_name,
        "x-ig-app-id": appId,
        "x-asbd-id": "359341"
    };
    if (root_field) headers["x-root-field-name"] = root_field;

    const res = await fetch(endpoint, {
        method: "POST",
        credentials: "include",
        headers: headers,
        body: body.toString()
    });

    const text = await res.text();
    return { status: res.status, body: text };
}
"""


async def run_graphql(page, *, friendly_name, doc_id, root_field, variables, endpoint="/graphql/query"):
    return await page.evaluate(GRAPHQL_JS, {
        "doc_id": doc_id,
        "friendly_name": friendly_name,
        "root_field": root_field,
        "variables": variables,
        "endpoint": endpoint,
    })


# ───────────────────────────── Comments Fetch JS ─────────────────────────────

COMMENTS_JS = """
async ({ pk, max_loops }) => {
    const html = document.documentElement.innerHTML;
    const pickFirst = (...regs) => {
        for (const r of regs) {
            const m = html.match(r);
            if (m) return m[1];
        }
        return null;
    };

    const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
    const appId     = pickFirst(/"X-IG-App-ID":"(\\d+)"/, /"APP_ID":"(\\d+)"/) || "936619743392459";
    const wwwClaim  = pickFirst(/"X-IG-WWW-Claim":"([^"]+)"/) || "0";

    const base = "/api/v1/media/" + pk + "/comments/";
    const headers = {
        "accept": "*/*",
        "x-asbd-id": "359341",
        "x-csrftoken": csrftoken,
        "x-ig-app-id": appId,
        "x-ig-www-claim": wwwClaim,
        "x-requested-with": "XMLHttpRequest"
    };

    let allComments = [];
    const seenPks = new Set();
    let minId = null;
    let loops = 0;
    let stopReason = "exhausted";

    while (loops < max_loops) {
        let url = base + "?can_support_threading=true";
        if (minId) {
            let token = null;
            try {
                token = JSON.parse(minId).bifilter_token;
            } catch (e) {
                token = null;
            }
            if (!token) { stopReason = "no_bifilter_token"; break; }
            const minIdParam = JSON.stringify({ bifilter_token: token });
            url += "&min_id=" + encodeURIComponent(minIdParam) + "&sort_order=popular";
        } else {
            url += "&permalink_enabled=false";
        }

        const res = await fetch(url, {
            method: "GET",
            credentials: "include",
            headers: headers
        });

        if (!res.ok) {
            stopReason = "http_" + res.status;
            break;
        }
        const data = await res.json();

        if (data.comments && data.comments.length > 0) {
            for (const c of data.comments) {
                if (seenPks.has(c.pk)) continue;
                seenPks.add(c.pk);
                allComments.push({
                    pk: c.pk,
                    media_id: c.media_id,
                    username: (c.user && c.user.username) || "",
                    text: c.text || "",
                    likes: c.comment_like_count || 0,
                    created_at: c.created_at_utc || 0
                });
            }
        }

        const next = data.next_min_id;
        const hasMore = data.has_more_headload_comments;
        if (next && hasMore) {
            if (next === minId) { stopReason = "same_min_id"; break; }
            minId = next;
        } else {
            stopReason = hasMore ? "no_next_min_id" : "has_more_false";
            break;
        }

        loops++;
    }

    return {
        count: allComments.length,
        loops: loops + 1,
        stop_reason: stopReason,
        comments: allComments
    };
}
"""


async def fetch_comments(page, pk: str, max_loops: int):
    return await page.evaluate(COMMENTS_JS, {"pk": pk, "max_loops": max_loops})


USER_ID_JS = """
({ username }) => {
    const html = document.documentElement.innerHTML;
    const patterns = [
        new RegExp('"profilePage_(\\\\d+)"'),
        new RegExp('"username":"' + username + '"[^}]{0,200}?"id":"(\\\\d+)"'),
        new RegExp('"id":"(\\\\d+)"[^}]{0,200}?"username":"' + username + '"'),
        /"user_id":"(\\d+)"/,
        /"owner_id":"(\\d+)"/
    ];
    for (const r of patterns) {
        const m = html.match(r);
        if (m) return m[1];
    }
    return null;
}
"""


async def extract_user_id(page, username: str) -> Optional[str]:
    return await page.evaluate(USER_ID_JS, {"username": username})


# ───────────────────────────── Endpoints ─────────────────────────────

@app.post("/inject_cookie")
async def inject_cookie(req: InjectCookieRequest):
    try:
        domain = req.domain.strip().rstrip("/")
        cookies = []
        for pair in req.cookie_string.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": req.path,
                "secure": req.secure,
                "sameSite": "Lax",
            })
        async with app.state.lock:
            page = await ensure_page()
            await page.context.add_cookies(cookies)
            return {"success": True, "count": len(cookies), "domain": domain, "timestamp": now_iso()}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/fetch-new")
async def fetch_new(req: ProfilePostsRequest):
    try:
        username = extract_username(req.url)
        if not username:
            return {"success": False, "error": "username parse failed", "timestamp": now_iso()}

        async with app.state.lock:
            page = await ensure_page()

            if username not in page.url:
                await page.goto(req.url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

            meta = await wait_for_query("first", timeout=10)

            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["username"] = username
            else:
                variables = {
                    "data": {
                        "count": 12,
                        "include_reel_media_seen_timestamp": True,
                        "include_relationship_info": True,
                        "latest_besties_reel_media": True,
                        "latest_reel_media": True,
                    },
                    "username": username,
                    "__relay_internal__pv__PolarisImmersiveFeedChainingEnabledrelayprovider": True,
                    "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                    "__relay_internal__pv__PolarisAIGMAccountLabelEnabledrelayprovider": False,
                }

            result = await run_graphql(
                page,
                friendly_name=meta["friendly_name"],
                doc_id=meta["doc_id"],
                root_field=meta["root_field"],
                variables=variables,
            )

            print(f"[fetch-new][{username}] status={result['status']} "
                  f"meta={meta['friendly_name']} len={len(result['body'])}")

            return {
                "success": True,
                "status": result["status"],
                "meta": {k: v for k, v in meta.items() if k != "variables_raw"},
                "data": result["body"],
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/fetch-next")
async def fetch_next(req: ProfilePostsNextRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()

            username = req.username or extract_username(page.url)
            if not username:
                return {"success": False, "error": "username not provided and parse failed",
                        "timestamp": now_iso()}

            if not find_query("next"):
                print("[fetch-next] next query not cached yet → trigger by scroll")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

            meta = await wait_for_query("next", timeout=10)

            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["after"] = req.after
                variables["username"] = username
            else:
                variables = {
                    "after": req.after,
                    "before": None,
                    "data": {
                        "count": 12,
                        "include_reel_media_seen_timestamp": True,
                        "include_relationship_info": True,
                        "latest_besties_reel_media": True,
                        "latest_reel_media": True,
                    },
                    "first": 12,
                    "last": None,
                    "username": username,
                }

            result = await run_graphql(
                page,
                friendly_name=meta["friendly_name"],
                doc_id=meta["doc_id"],
                root_field=meta["root_field"],
                variables=variables,
            )

            print(f"[fetch-next][{username}] after={req.after[:20]}... "
                  f"status={result['status']} meta={meta['friendly_name']} "
                  f"len={len(result['body'])}")
            return {
                "success": True,
                "status": result["status"],
                "meta": {k: v for k, v in meta.items() if k != "variables_raw"},
                "data": result["body"],
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.get("/debug/cache")
async def debug_cache():
    return {"cache": QUERY_CACHE, "timestamp": now_iso()}


@app.post("/comment")
async def comment(req: CommentRequest):
    try:
        async with app.state.lock:
            page = await ensure_page()

            if req.code:
                target = f"https://www.instagram.com/p/{req.code}/"
                if req.code not in page.url:
                    await page.goto(target, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
            else:
                if "instagram.com" not in (page.url or ""):
                    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)

            result = await fetch_comments(page, req.pk, req.max_loops)

            print(f"[comment][pk={req.pk}] count={result['count']} "
                  f"loops={result['loops']} stop={result['stop_reason']}")
            return {
                "success": True,
                "pk": req.pk,
                "count": result["count"],
                "loops": result["loops"],
                "stop_reason": result["stop_reason"],
                "comments": result["comments"],
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.post("/profile")
async def profile(req: ProfileRequest):
    try:
        if not req.url and not req.id:
            return {"success": False,
                    "error": "either 'url' or 'id' is required",
                    "timestamp": now_iso()}

        async with app.state.lock:
            page = await ensure_page()

            user_id = req.id
            username = req.username

            if req.url:
                username = extract_username(req.url) or username
                if username and username not in page.url:
                    await page.goto(req.url, wait_until="domcontentloaded")
                    await asyncio.sleep(2)

                if not user_id and username:
                    user_id = await extract_user_id(page, username)

            if not req.url and "instagram.com" not in (page.url or ""):
                await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
                await asyncio.sleep(1.5)

            if not user_id:
                return {"success": False,
                        "error": f"user_id resolve failed (username={username})",
                        "timestamp": now_iso()}

            meta = await wait_for_query("profile", timeout=10)

            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["id"] = user_id
            else:
                variables = {
                    "enable_integrity_filters": True,
                    "id": user_id,
                }

            result = await run_graphql(
                page,
                friendly_name=meta["friendly_name"],
                doc_id=meta["doc_id"],
                root_field=meta.get("root_field", ""),
                variables=variables,
                endpoint=meta.get("endpoint", "/api/graphql"),
            )

            print(f"[profile][id={user_id} username={username}] "
                  f"status={result['status']} meta={meta['friendly_name']} "
                  f"len={len(result['body'])}")
            return {
                "success": True,
                "status": result["status"],
                "user_id": user_id,
                "username": username,
                "meta": {k: v for k, v in meta.items() if k != "variables_raw"},
                "data": result["body"],
                "timestamp": now_iso(),
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}

EDGES_PATH = "xdt_api__v1__feed__user_timeline_graphql_connection"
PAGE_SIZE = 12


def _parse_date_floor(date_str: str) -> int:
    """'YYYY-MM-DD' → 당일 00:00:00 로컬 기준 unix timestamp(초). 당일 포함(>=)."""
    dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    return int(dt.timestamp())


def _extract_edges(body_str: str) -> list:
    """run_graphql 의 body(JSON 문자열) → edges 리스트."""
    try:
        parsed = json.loads(body_str)
    except Exception:
        return []
    conn = (parsed.get("data") or {}).get(EDGES_PATH) or {}
    return conn.get("edges") or []


@app.post("/instagram")
async def instagram(req: InstagramRequest):
    try:
        username = req.username.strip().strip("/")
        if not username:
            return {"success": False, "error": "username required", "timestamp": now_iso()}

        date_floor = _parse_date_floor(req.date) if req.date else None
        profile_url = f"https://www.instagram.com/{username}/"

        collected: list = []
        stop_reason = "exhausted"
        date_hit = False

        async with app.state.lock:
            page = await ensure_page()

            # ── 프로필 페이지 진입 ──
            if username not in (page.url or ""):
                await page.goto(profile_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

            # ── 1) fetch-new (첫 페이지) ──
            meta = await wait_for_query("first", timeout=10)
            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["username"] = username
            else:
                variables = {
                    "data": {
                        "count": PAGE_SIZE,
                        "include_reel_media_seen_timestamp": True,
                        "include_relationship_info": True,
                        "latest_besties_reel_media": True,
                        "latest_reel_media": True,
                    },
                    "username": username,
                    "__relay_internal__pv__PolarisImmersiveFeedChainingEnabledrelayprovider": True,
                    "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                    "__relay_internal__pv__PolarisAIGMAccountLabelEnabledrelayprovider": False,
                }

            result = await run_graphql(
                page,
                friendly_name=meta["friendly_name"],
                doc_id=meta["doc_id"],
                root_field=meta["root_field"],
                variables=variables,
            )
            edges = _extract_edges(result["body"])

            # 첫 페이지 처리
            page_full = len(edges) >= PAGE_SIZE
            for edge in edges:
                node = edge.get("node") or {}
                taken_at = node.get("taken_at") or 0
                if date_floor is not None and taken_at < date_floor:
                    date_hit = True
                    break
                collected.append(edge)
                if req.count is not None and len(collected) >= req.count:
                    break

            # 종료 판단
            if date_hit:
                stop_reason = "date_boundary"
            elif req.count is not None and len(collected) >= req.count:
                stop_reason = "count_reached"
            elif not page_full:
                stop_reason = "exhausted"
            else:
                # ── 2) fetch-next 반복 ──
                after = edges[-1]["node"]["id"]

                while True:
                    if not find_query("next"):
                        print("[instagram] next query not cached yet → trigger by scroll")
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)

                    nmeta = await wait_for_query("next", timeout=10)
                    if nmeta.get("variables_raw"):
                        nvars = json.loads(nmeta["variables_raw"])
                        nvars["after"] = after
                        nvars["username"] = username
                    else:
                        nvars = {
                            "after": after,
                            "before": None,
                            "data": {
                                "count": PAGE_SIZE,
                                "include_reel_media_seen_timestamp": True,
                                "include_relationship_info": True,
                                "latest_besties_reel_media": True,
                                "latest_reel_media": True,
                            },
                            "first": PAGE_SIZE,
                            "last": None,
                            "username": username,
                        }

                    nresult = await run_graphql(
                        page,
                        friendly_name=nmeta["friendly_name"],
                        doc_id=nmeta["doc_id"],
                        root_field=nmeta["root_field"],
                        variables=nvars,
                    )
                    nedges = _extract_edges(nresult["body"])
                    npage_full = len(nedges) >= PAGE_SIZE

                    for edge in nedges:
                        node = edge.get("node") or {}
                        taken_at = node.get("taken_at") or 0
                        if date_floor is not None and taken_at < date_floor:
                            date_hit = True
                            break
                        collected.append(edge)
                        if req.count is not None and len(collected) >= req.count:
                            break

                    if date_hit:
                        stop_reason = "date_boundary"
                        break
                    if req.count is not None and len(collected) >= req.count:
                        stop_reason = "count_reached"
                        break
                    if not npage_full:
                        stop_reason = "exhausted"
                        break
                    if not nedges:
                        stop_reason = "exhausted"
                        break

                    after = nedges[-1]["node"]["id"]

        # ── 최종 자르기 (count 상한) ──
        if req.count is not None:
            collected = collected[:req.count]

        print(f"[instagram][{username}] count={len(collected)} "
              f"stop={stop_reason} date={req.date} req_count={req.count}")

        return {
            "success": True,
            "username": username,
            "count": len(collected),
            "stop_reason": stop_reason,
            "date": req.date,
            "data": collected,
            "timestamp": now_iso(),
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"success": False, "error": str(e), "timestamp": now_iso()}


@app.get("/debug/screenshot")
async def debug_screenshot():
    async with app.state.lock:
        page = await ensure_page()
        png = await page.screenshot(full_page=False)
        return Response(content=png, media_type="image/png")

@app.get("/debug/url")
async def debug_url():
    async with app.state.lock:
        page = await ensure_page()
        return {"url": page.url, "title": await page.title()}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8026)
