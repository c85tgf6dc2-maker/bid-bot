import os
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import psycopg2
import requests

BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
RAW_SERVICE_KEY = os.environ.get("DATA_GO_KR_SERVICE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# 공공데이터포털의 Encoding 키(%2F, %3D 등)를 Secret에 넣어도
# requests가 다시 인코딩하지 않도록 먼저 Decoding 형태로 변환한다.
SERVICE_KEY = unquote(RAW_SERVICE_KEY) if RAW_SERVICE_KEY else None

KST = timezone(timedelta(hours=9))
TARGET_REGION = "광양"

A_FIELDS = [
    "sftyMngcst",              # 산업안전보건관리비
    "sftyChckMngcst",          # 안전관리비
    "rtrfundNon",              # 퇴직공제부금비
    "mrfnHealthInsrprm",       # 국민건강보험료
    "npnInsrprm",              # 국민연금보험료
    "odsnLngtrmrcprInsrprm",   # 노인장기요양보험료
    "qltyMngcst",              # 품질관리비
]


def as_number(value):
    if value in (None, ""):
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def api_get(path, params):
    if not SERVICE_KEY:
        raise RuntimeError("DATA_GO_KR_SERVICE_KEY secret is missing")

    query = dict(params)
    query.update({
        "serviceKey": SERVICE_KEY,
        "type": "json",
    })

    response = requests.get(BASE_URL + path, params=query, timeout=40)
    if not response.ok:
        raise RuntimeError(
            f"G2B HTTP {response.status_code}: {response.text[:500]}"
        )

    data = response.json()

    header = data.get("response", {}).get("header", {})
    if header.get("resultCode") != "00":
        raise RuntimeError(
            f"G2B API error: {header.get('resultCode')} {header.get('resultMsg')}"
        )

    body = data.get("response", {}).get("body", {})
    items = body.get("items", []) or []
    if isinstance(items, dict):
        items = [items]
    return items, int(body.get("totalCount", len(items)) or 0)


def get_recent_construction_bids():
    now = datetime.now(KST)
    begin = now - timedelta(hours=6)

    common = {
        "inqryDiv": 1,
        "inqryBgnDt": begin.strftime("%Y%m%d%H%M"),
        "inqryEndDt": now.strftime("%Y%m%d%H%M"),
        "pageNo": 1,
        "numOfRows": 100,
    }

    first, total = api_get("/getBidPblancListInfoCnstwk", common)
    rows = list(first)

    page = 2
    while len(rows) < total:
        params = dict(common)
        params["pageNo"] = page
        page_rows, _ = api_get("/getBidPblancListInfoCnstwk", params)
        if not page_rows:
            break
        rows.extend(page_rows)
        page += 1

    return rows


def get_by_notice(path, notice_no, rows=100):
    params = {
        "pageNo": 1,
        "numOfRows": rows,
        "inqryDiv": 2,
        "bidNtceNo": notice_no,
    }
    result, _ = api_get(path, params)
    return result


def all_text(*objects):
    return " ".join(str(obj) for obj in objects if obj)


def is_target_industry(text):
    text = text.replace("ㆍ", "·")
    paving = "지반조성" in text and "포장" in text
    coating_group = any(word in text for word in ["도장", "습식", "방수", "석공"])
    return paving or coating_group


def calculate_a_value(a_rows, basis_rows):
    source = a_rows[0] if a_rows else (basis_rows[0] if basis_rows else {})
    if not source:
        return None

    total = sum(as_number(source.get(field)) for field in A_FIELDS)
    return total if total > 0 else None


def classify_target(bid, license_rows, region_rows, field_rows):
    region_text = all_text(
        bid.get("cnstrtsiteRgnNm", ""),
        bid.get("ntceInsttNm", ""),
        region_rows,
    )
    if TARGET_REGION not in region_text:
        return False

    industry_text = all_text(
        bid.get("mainCnsttyNm", ""),
        license_rows,
        field_rows,
    )
    return is_target_industry(industry_text)


def save_bid(conn, bid, basis_rows, a_value):
    basis = basis_rows[0] if basis_rows else {}

    notice_no = f"{bid.get('bidNtceNo')}-{bid.get('bidNtceOrd', '000')}"
    base_price = basis.get("bssamt") or None
    bid_rate = bid.get("sucsfbidLwltRate") or None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.bids (
                notice_no,
                agency,
                project_name,
                category,
                region,
                base_price,
                a_value,
                bid_rate,
                announced_at,
                source_url
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (notice_no)
            DO UPDATE SET
                agency = EXCLUDED.agency,
                project_name = EXCLUDED.project_name,
                category = EXCLUDED.category,
                region = EXCLUDED.region,
                base_price = EXCLUDED.base_price,
                a_value = EXCLUDED.a_value,
                bid_rate = EXCLUDED.bid_rate,
                announced_at = EXCLUDED.announced_at,
                source_url = EXCLUDED.source_url,
                updated_at = NOW()
            """,
            (
                notice_no,
                bid.get("ntceInsttNm"),
                bid.get("bidNtceNm"),
                bid.get("mainCnsttyNm"),
                bid.get("cnstrtsiteRgnNm"),
                base_price,
                a_value,
                bid_rate,
                bid.get("bidNtceDt") or None,
                bid.get("bidNtceDtlUrl") or None,
            ),
        )


def main():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL secret is missing")

    bids = get_recent_construction_bids()
    print(f"최근 공사 공고: {len(bids)}건")

    conn = psycopg2.connect(DATABASE_URL)
    saved = 0

    try:
        for bid in bids:
            notice_no = bid.get("bidNtceNo")
            if not notice_no:
                continue

            try:
                license_rows = get_by_notice("/getBidPblancListInfoLicenseLimit", notice_no)
                region_rows = get_by_notice("/getBidPblancListInfoPrtcptPsblRgn", notice_no)
                field_rows = get_by_notice("/getBidPblancListEvaluationIndstrytyMfrcInfo", notice_no)

                if not classify_target(bid, license_rows, region_rows, field_rows):
                    continue

                basis_rows = get_by_notice("/getBidPblancListInfoCnstwkBsisAmount", notice_no, rows=10)
                a_rows = get_by_notice("/getBidPblancListBidPrceCalclAInfo", notice_no, rows=10)
                a_value = calculate_a_value(a_rows, basis_rows)

                save_bid(conn, bid, basis_rows, a_value)
                conn.commit()
                saved += 1

                print(
                    "저장:",
                    f"{notice_no}-{bid.get('bidNtceOrd', '000')}",
                    bid.get("bidNtceNm"),
                    "| 업종:", bid.get("mainCnsttyNm"),
                    "| 기초:", (basis_rows[0].get("bssamt") if basis_rows else None),
                    "| A:", a_value,
                    "| 하한율:", bid.get("sucsfbidLwltRate"),
                )

            except Exception as exc:
                conn.rollback()
                print(f"공고 처리 실패 {notice_no}: {exc}")

    finally:
        conn.close()

    print(f"완료: 대상 공고 {saved}건 저장")


if __name__ == "__main__":
    main()
