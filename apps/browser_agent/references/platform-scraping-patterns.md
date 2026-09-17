# Platform Scraping Patterns

These patterns are distilled from real B站、小红书、抖音、微博 collection work. Use them when building public-content collectors or debugging site automation.

## Universal Pipeline

1. **Capture first, aggregate later**: browser scripts should output raw JSON arrays. Time parsing, relevance filtering, dedupe, and report generation belong in a separate aggregation step.
2. **Use visible mode by default**: external sites often need login/captcha handoff. Use headless only for unattended jobs that can skip blocked platforms.
3. **Persist profile**: use a dedicated automation profile such as `~/.cloakbrowser-profile`; do not reuse or close the user's main Chrome.
4. **Reuse same-domain tabs**: before opening a new page, look for an existing tab with the same domain and navigate it. This avoids tab spam and preserves interactive state.
5. **Record evidence**: save screenshots before extraction rounds, after sort-click verification, and on captcha/login/selector errors.
6. **Normalize extractor output**: every item should prefer `{title, text, author, time, link, metrics, id}` where fields may be empty but keys stay stable.
7. **Dedupe per platform before aggregation**: link/id is best; fallback to a normalized text prefix when links are absent or unstable.

## Anti-Detection and Engine Choice

Prefer `cloak` for these platforms:

- CloakBrowser removes automation fingerprints at the Chromium layer, not only by JS injection.
- It avoids CDP `cdc_` traces and does not require closing the user's main Chrome.
- It works well with persistent cookies and visible user handoff.

Fallback order remains `cloak > browser-act > kimi > playwright`. If CloakBrowser fails because of missing dependencies, install lazily or switch according to the task policy. If it fails because of login/captcha, first use human handoff once before switching. If browser-act is selected for the first time on a device, install it lazily only when the caller explicitly opted into install.

## Sorting and Navigation

Use URL parameters where they are reliable, but verify sort state when the site may ignore them.

| Platform | Preferred entry | Sort / paging strategy |
|---|---|---|
| 小红书 | `https://www.xiaohongshu.com/search_result?keyword=<kw>&type=51` | `type=51` means latest; still verify/click latest when available. |
| 抖音 | `https://www.douyin.com/search/<kw>?type=video&sort_type=2` | Latest sort may need DOM click verification; use fewer scroll rounds. |
| 微博 | `https://s.weibo.com/realtime?q=<kw>&rd=realtime&tw=realtime&Refer=weibo_realtime` | Prefer direct `&page=N` URL paging; do not rely on infinite scroll. |
| B站 | `https://search.bilibili.com/all?keyword=<kw>&order=pubdate` | `order=pubdate` is reliable; use scroll rounds for lazy-loaded cards. |

## Platform Patterns

### 小红书

- Detection strength is high; require persistent login for reliable capture.
- Search result card roots are not always at a fixed parent depth. Start from `a[href*="/explore/"]`, climb parent nodes until `innerText.length` is in a conservative range such as `20..400`, then extract inside that root.
- Use note id from `/explore/<id>` for dedupe; fallback to link.
- Extract title from `a.title`, author from `div.name`, time from `div.time`, likes from `.like-wrapper .count`.
- Keep full card text for downstream relevance filtering; many cards have short or missing titles.

Pitfalls:

- Do not hardcode "go up N parents"; it can select the whole page container.
- Login wall can appear as modal rather than URL redirect, so combine URL and DOM checks.

### 抖音

- Detection strength is high and multi-round scrolling can trigger captcha. Start with 2 rounds, longer pauses, and smaller scroll distances.
- DOM changes frequently. Use multiple selectors in order: specific `data-e2e` search card selectors, generic search/video card classes, list items, and finally text-block fallback.
- Use non-capturing alternation for Chinese time units: `(?:分钟|小时|天|周|月|年)前`. Do not write `[分钟小时天]`; that is a character class and causes false matches.
- Same card may be captured at parent and grandparent levels. Dedupe by normalized text prefix after stripping duration/like prefixes and `@作者·时间` suffixes.
- Extract author from `@name`, link from first `a[href]`, time from text, likes from `赞/♥` patterns.

Pitfalls:

- If captcha appears after scrolling, preserve existing items and stop that platform; do not continue hammering.
- URL sort parameters may be ignored; verify visible sort state when accuracy matters.

### 微博

- Realtime search is more stable with URL paging than scroll.
- Use `div.card-wrap[mid], div.card-wrap` as card roots.
- Extract author from `a.name` or `div.info a[nick-name]`.
- Extract text from `p.txt[node-type="feed_list_content_full"]`, fallback to `feed_list_content`, then `p.txt`.
- Extract time/link from the first `div.from a[href*="weibo.com"]`.
- Dedupe by link; fallback to the first 40 characters of text.

Pitfalls:

- Login may be required even when the search page partially renders.
- Some cards are promotions or empty wrappers; keep `if text or author` guard.

### B站

- Search pages are SPA lazy-loaded; scroll to trigger more cards.
- `order=pubdate` reliably requests newest content.
- Select `div.bili-video-card:not([class*="skeleton"])` to exclude placeholder skeletons.
- Reject cards with extreme text lengths, e.g. `<10` or `>600`, to avoid selecting wrappers.
- Extract title from `h3.bili-video-card__info--tit`, link from `a[href*="bilibili.com/video"], a[href*="/video/"]`, author from `span.bili-video-card__info--author`, date from `span.bili-video-card__info--date` and strip leading `·`.
- Dedupe by link.

Pitfalls:

- B站 time strings often begin with `· `. Strip it before parsing.
- Chinese constants inside JS that will be base64/eval/powershell embedded should use `\uXXXX` escapes to avoid encoding corruption.

## Time Parsing and Relevance Filtering

Implement downstream aggregation to handle:

- `X秒前`, `刚刚`, `X分钟前`, `X小时前`, `X天前`, `昨天`, `今天HH:MM`, `MM-DD`, `MM-DD HH:MM`.
- B站 leading `· ` before time.
- Direct keyword hits in the title/text should pass first.
- For product ecosystems, maintain a strong-keyword set for related terms; otherwise public search results include heavy noise.

## Verification Checklist

- Raw files are valid JSON arrays.
- Every item has either `link` or a stable fallback dedupe key.
- Sort state has screenshot evidence when a DOM sort click is used.
- Captcha/login screenshots are saved when blocked.
- Aggregation handles 0 results for a platform without failing the whole job.
- Final report links are clickable and include source platform labels.

## Continuous Experience Capture

After every real platform scraping run, decide whether a reusable lesson should be recorded. Record only stable, future-useful lessons, not transient network failures.

Record a note when:

- A selector, URL parameter, paging, sorting, or scroll strategy changed.
- A platform-specific login/captcha/anti-bot behavior appeared.
- A dedupe/time parsing/relevance filtering rule was corrected.
- A fallback engine materially changed the outcome.
- A verification method proved reliable or unreliable.

Preferred command:

```bash
python scripts/record_platform_experience.py --platform xhs --lesson "父容器不能固定上溯层数" --evidence "fixed depth selected whole page wrapper" --action "climb until innerText length is 20..400"
```

Manual format if editing this file directly:

```text
- YYYY-MM-DD / source: <site or task> / lesson: <what changed> / evidence: <how observed> / action: <what to do next time>
```

## Accumulated Lessons

### 小红书

- 2026-06-15 / source: field-tested scraping / lesson: 排序切换必须用 browser-act `click` 而非 JS eval / evidence: JS dispatchEvent 无法触发 React SPA 状态更新，筛选弹窗中点击"最新"不会切换 active 状态；browser-act click（CDP 原生鼠标事件）成功将"最新"置为 active 并触发数据重载 / action: 切换小红书排序时，先 `state` 找元素 idx，再用 `browser-act --session X click <idx>`，不要用 `eval` 注入合成事件。

### 抖音

### 微博

### B站

### 通用
