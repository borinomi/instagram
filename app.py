import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
    STEALTH_MODE = "legacy"
except ImportError:
    from playwright_stealth import Stealth
    STEALTH_MODE = "context"

CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
CDP_HOST = os.getenv("CDP_HOST", socket.gethostbyname("host.docker.internal"))


def now_iso():
    return datetime.now().isoformat()


# ───────────────────────────── Models ─────────────────────────────

class ProfilePostsRequest(BaseModel):
    url: str


class ProfilePostsNextRequest(BaseModel):
    after: str
    username: Optional[str] = None

class CommentRequest(BaseModel):
    pk: str                              # 게시물 media id
    code: Optional[str] = None           # 게시물 shortcode (referrer 용, 없어도 됨)
    max_loops: int = 50                  # 안전장치

class ProfileRequest(BaseModel):
    url: Optional[str] = None        # https://www.instagram.com/lg_uk/
    id: Optional[str] = None         # user_id (50918045). 있으면 페이지 이동 생략 가능
    username: Optional[str] = None   # 디버깅/로깅용


# ───────────────────────────── Stealth ─────────────────────────────

async def create_stealth_page(context):
    if STEALTH_MODE == "legacy":
        page = await context.new_page()
        await stealth_async(page)
        return page
    stealth = Stealth()
    await stealth.apply_stealth_async(context)
    return await context.new_page()


# ───────────────────────────── Query Cache ─────────────────────────────
#
# 페이지가 실제로 날리는 GraphQL 요청을 가로채서
# friendly_name → {doc_id, root_field} 매핑을 저장.

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
        variables_raw = (parsed.get("variables") or [None])[0]  # ← 추가
        
        if not friendly:
            friendly = (parsed.get("fb_api_req_friendly_name") or [None])[0]

        endpoint = "/api/graphql" if "/api/graphql" in req.url else "/graphql/query"

        if friendly and doc_id:
            QUERY_CACHE[friendly] = {
                "doc_id": doc_id,
                "root_field": root_field or "",
                "endpoint": endpoint,
                "variables_raw": variables_raw,  # ← 추가
            }
            print(f"[harvest] {friendly} → doc_id={doc_id} ep={endpoint} root_field={root_field}")
    except Exception as e:
        print(f"[harvest] error: {e}")


# ───────────────────────────── Lifespan ─────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright = await async_playwright().start()
    app.state.playwright = playwright
    app.state.browser = None
    app.state.context = None
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
        await playwright.stop()


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

    browser = await app.state.playwright.chromium.connect_over_cdp(
        f"http://{CDP_HOST}:{CDP_PORT}"
    )
    if not browser.contexts:
        raise RuntimeError("browser context not found")

    context = browser.contexts[0]
    page = await create_stealth_page(context)

    # GraphQL 요청 가로채기
    page.on("request", _harvest_request)

    app.state.browser = browser
    app.state.context = context
    app.state.page = page
    return page


async def ensure_page():
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
    """캐시에 해당 kind 쿼리가 들어올 때까지 대기."""
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
            // next_min_id 에서 bifilter_token 만 빼서 재포장
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
    // 여러 패턴 시도 — 인스타 페이지 빌드마다 위치가 다를 수 있음
    const patterns = [
        // "profilePage_50918045" 같은 형태
        new RegExp('"profilePage_(\\\\d+)"'),
        // "owner":{"id":"50918045", ... ,"username":"lg_uk"}
        new RegExp('"username":"' + username + '"[^}]{0,200}?"id":"(\\\\d+)"'),
        new RegExp('"id":"(\\\\d+)"[^}]{0,200}?"username":"' + username + '"'),
        // "user_id":"50918045"
        /"user_id":"(\\d+)"/,
        // "owner_id":"50918045"
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

@app.post("/fetch-new")
async def fetch_new(req: ProfilePostsRequest):
    try:
        username = extract_username(req.url)
        if not username:
            return {"success": False, "error": "username parse failed",
                    "timestamp": now_iso()}

        async with app.state.lock:
            page = await ensure_page()

            if username not in page.url:
                await page.goto(req.url, wait_until="domcontentloaded")
                await asyncio.sleep(2)

            meta = await wait_for_query("first", timeout=10)

            # 인스타가 실제로 보낸 variables를 그대로 사용
            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                # username만 우리가 원하는 걸로 덮어쓰기 (다른 계정 조회용)
                variables["username"] = username
            else:
                # 폴백
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

            # next 쿼리 메타가 아직 캐시에 없으면 스크롤로 트리거
            if not find_query("next"):
                print("[fetch-next] next query not cached yet → trigger by scroll")
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(2)

            meta = await wait_for_query("next", timeout=10)

            # 캐시된 variables 그대로 사용, after와 username만 덮어쓰기
            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["after"] = req.after
                variables["username"] = username
            else:
                # 폴백
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

            # referrer 페이지로 이동 (쿠키/세션 컨텍스트 확보)
            if req.code:
                target = f"https://www.instagram.com/p/{req.code}/"
                if req.code not in page.url:
                    await page.goto(target, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
            else:
                # code 가 없으면 현재 페이지 컨텍스트 그대로 사용
                # (인스타 도메인 페이지에 한 번이라도 들어와 있어야 쿠키/토큰 추출 가능)
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

            # 캐시된 variables 그대로 사용, id만 덮어쓰기
            if meta.get("variables_raw"):
                variables = json.loads(meta["variables_raw"])
                variables["id"] = user_id
            else:
                # 폴백
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8026)
