import os
import re
import asyncio
import requests
from playwright.async_api import async_playwright

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

HERMES_TW_BAGS_URL = "https://www.hermes.com/tw/zh/category/leather-goods/bags-and-clutches/womens-bags-and-clutches/"
REDIS_KEY = "hermes:seen_bags"

def extract_product_id(url):
    """從愛馬仕商品網址中提取商品 ID (例如 H087970CT18)"""
    match = re.search(r'-(H[A-Z0-9]+)', url)
    if match:
        return match.group(1)
    
    clean_url = url.rstrip('/')
    last_segment = clean_url.split('/')[-1]
    return re.sub(r'[^a-zA-Z0-9_-]', '', last_segment)

def send_telegram_notification(title, link):
    """傳送 Telegram 通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳過發送。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    message_text = (
        f"🚨 <b>Hermès 官網補貨/新上架通知！</b>\n\n"
        f"👜 <b>{title}</b>\n"
        f"🔗 <a href='{link}'>點此前往官網搶購</a>"
    )
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✅ Telegram 通知發送成功: {title}")
    except Exception as e:
        print(f"❌ Telegram 發送失敗 ({title}): {e}")

def upstash_command(command, *args):
    """封裝 Upstash REST API 請求"""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        print("⚠️ 未設定 Upstash Redis URL 或 Token，無法進行數據去重。")
        return None

    clean_args = [str(arg).strip('/') for arg in args]
    url = f"{UPSTASH_URL.rstrip('/')}/{command}/" + "/".join(clean_args)
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json().get("result")
    except Exception as e:
        print(f"❌ Upstash API 呼叫失敗 ({command}): {e}")
        return None

def get_current_seen_ids():
    """取得 Redis 中目前紀錄的所有商品 ID (SMEMBERS)"""
    result = upstash_command("smembers", REDIS_KEY)
    if isinstance(result, list):
        return set(result)
    return set()

def replace_seen_ids(product_ids):
    """以當前線上的商品 ID 清單全量覆蓋 Redis (DEL + SADD)"""
    # 1. 刪除舊有集合
    upstash_command("del", REDIS_KEY)
    
    # 2. 寫入最新的商品 ID 集合
    if product_ids:
        upstash_command("sadd", REDIS_KEY, *list(product_ids))

async def fetch_hermes_bags():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox'
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="zh-TW",
            timezone_id="Asia/Taipei"
        )
        
        page = await context.new_page()
        
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        print(f"🌐 正在前往愛馬仕官網包款專區...")
        
        try:
            await page.goto(HERMES_TW_BAGS_URL, wait_until="networkidle", timeout=60000)
            
            try:
                await page.wait_for_selector("a[href*='/product/']", timeout=20000)
            except Exception:
                print("⚠️ 等待 Timeout，強制向下滾動頁面...")
            
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(2)

            product_links = await page.query_selector_all("a[href*='/product/']")
            
            bags = []
            seen_urls = set()

            for elem in product_links:
                href = await elem.get_attribute("href")
                if not href:
                    continue
                
                full_url = href if href.startswith("http") else f"https://www.hermes.com{href}"
                
                if full_url in seen_urls:
                    continue
                
                product_id = extract_product_id(full_url)
                
                raw_text = await elem.inner_text()
                text_lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
                
                title = text_lines[0] if text_lines else "Hermès 包款"
                price = text_lines[1] if len(text_lines) > 1 else ""
                full_title = f"{title} ({price})" if price else title
                
                seen_urls.add(full_url)
                bags.append({
                    "id": product_id,
                    "title": full_title,
                    "link": full_url
                })

            return bags

        except Exception as e:
            print(f"❌ 抓取失敗: {e}")
            await page.screenshot(path="error_screenshot.png")
            return None  # 注意：爬蟲出錯時傳回 None，避免清空 Redis
        finally:
            await browser.close()

async def main():
    print("🚀 啟動 Hermès 補貨監控任務...")
    bags = await fetch_hermes_bags()
    
    # 防護機制：若爬蟲失敗 (bags 為 None)，不進行 Redis 全量覆蓋，保護舊數據
    if bags is None:
        print("⚠️ 爬蟲抓取異常，終止本次更新以維持 Redis 數據完整性。")
        return

    print(f"🎉 成功抓取到 {len(bags)} 件線上商品！\n")

    # 1. 取得 Redis 中上一輪記錄的商品 ID 清單
    previous_seen_ids = get_current_seen_ids()
    current_product_ids = {bag["id"] for bag in bags}

    new_count = 0
    # 2. 比對找出本次全新上架/重新補貨的商品
    for bag in bags:
        product_id = bag["id"]
        link = bag["link"]
        title = bag["title"]

        if product_id not in previous_seen_ids:
            print(f"✨ 發現新上架/補貨商品 [{product_id}]: {title}")
            send_telegram_notification(title, link)
            new_count += 1
        else:
            print(f"ℹ️ 已在線上且通知過，跳過 [{product_id}]: {title}")

    # 3. 關鍵機制：以當前線上商品的 ID 組合，全量覆蓋更新 Redis
    replace_seen_ids(current_product_ids)
    print("\n💾 Redis 已更新為當前最新的線上商品清單。")

    if new_count == 0:
        print("✅ 本次檢查完畢，未發現任何全新上架或重新補貨的商品。")
    else:
        print(f"🎉 本次檢查完畢，共發送了 {new_count} 則補貨通知！")

if __name__ == "__main__":
    asyncio.run(main())