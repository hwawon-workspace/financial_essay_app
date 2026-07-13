"""
금융공기업 논술 트레이너 (bank_essay_trainer.py)
실행: streamlit run bank_essay_trainer.py

동작 방식:
- 하루에 문제는 기본적으로 1개로 고정된다 (같은 날 다시 켜도 같은 문제가 뜸).
- "새로 생성하기" 버튼을 누르면 그날의 문제를 새로 만들어 덮어쓴다.
- 채점 또는 교정 결과가 나올 때마다 자동으로 Google Sheets(History)에 기록된다.
- Google Sheets 미설정 시에도 앱은 정상 작동하며, 하루 고정/자동 기록만 비활성화된다.
"""

import streamlit as st
import pandas as pd
import requests
import feedparser
import datetime
import random
from openai import OpenAI
import concurrent.futures
from zoneinfo import ZoneInfo


client = OpenAI(
    api_key=st.secrets["UPSTAGE_API_KEY"],
    base_url="https://api.upstage.ai/v1",
)


def call_solar(prompt, temperature=0.4, max_tokens=1800):
    try:
        resp = client.chat.completions.create(
            model="solar-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"[LLM 호출 오류] {e}"


INSTITUTIONS = {
    "한국은행": {
        "focus": ["통화정책", "금리/물가", "국제금융", "거시경제", "디지털화폐(CBDC)"],
        "style": "제시문(통계·그래프 포함) 기반 서술형, 정책 판단력 요구",
        "days": ["월"], "ecos_indicators": ["기준금리", "소비자물가지수"],
    },
    "한국산업은행(KDB)": {
        "focus": ["산업정책", "구조조정", "정책금융", "ESG", "벤처/스타트업 금융"],
        "style": "산업/기업 사례 제시문 + 정책금융기관 역할 논술",
        "days": ["화"], "ecos_indicators": [],
    },
    "금융감독원": {
        "focus": ["금융소비자보호", "금융회사 건전성", "가계부채", "금융사고/내부통제", "자본시장 불공정거래"],
        "style": "사회 이슈 + 감독정책 결합, 균형잡힌 시각과 대안 제시 요구",
        "days": ["수"], "ecos_indicators": [],
    },
    "한국거래소(KRX)": {
        "focus": ["자본시장 활성화", "상장기업 밸류업", "공매도", "가상자산/디지털자산", "IPO/증시제도"],
        "style": "자본시장 현안 제시문, 제도 개선안 논술",
        "days": ["목"], "ecos_indicators": [],
    },
    "한국수출입은행(KEXIM)": {
        "focus": ["수출금융", "대외경제협력", "공급망/글로벌 통상", "개발금융", "환율/국제수지"],
        "style": "국제경제·통상 이슈 제시문, 정책금융기관 대응방안 논술",
        "days": ["금"], "ecos_indicators": ["원/달러 환율"],
    },
    "시중은행(4대銀 등)": {
        "focus": ["가계/기업금융 트렌드", "디지털뱅킹·AI 활용", "상생금융", "리스크관리"],
        "style": "실무 현안 + 은행의 사회적 역할 논술",
        "days": ["토"], "ecos_indicators": ["기준금리"],
    },
}

RSS_SOURCES = {
    "금융위원회 보도자료": "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0111",
    "금융위원회 보도설명": "http://www.fsc.go.kr/about/fsc_bbs_rss/?fid=0112",
    "국회예산정책처 보도자료": "https://www.nabo.go.kr/rss/pressRelease.do",
    "국회예산정책처 발간물": "https://www.nabo.go.kr/rss/publications.do",
    "통계청 보도자료": "https://kostat.go.kr/board.es?mid=a10301010000&bid=a103010100&act=rss",
}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_daily_articles(max_items=5):
    def fetch_one(name_url):
        name, url = name_url
        rows = []
        try:
            resp = requests.get(url, timeout=4, headers={"User-Agent": "Mozilla/5.0"})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:max_items]:
                rows.append({
                    "출처": name,
                    "제목": entry.get("title", "").strip(),
                    "링크": entry.get("link", ""),
                    "게시일": entry.get("published", entry.get("updated", "")),
                })
        except Exception:
            rows.append({"출처": name, "제목": "[접속 지연/차단으로 수집 실패]", "링크": "", "게시일": ""})
        return rows

    all_rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_one, item): item for item in RSS_SOURCES.items()}
        for future in concurrent.futures.as_completed(futures, timeout=8):
            try:
                all_rows.extend(future.result(timeout=0.1))
            except Exception:
                name = futures[future][0]
                all_rows.append({"출처": name, "제목": "[시간초과]", "링크": "", "게시일": ""})
    return pd.DataFrame(all_rows)


ECOS_INDICATORS = {
    "기준금리": {"stat_code": "722Y001", "item_code": "0101000", "unit": "%"},
    "원/달러 환율": {"stat_code": "731Y001", "item_code": "0000001", "unit": "원"},
    "소비자물가지수": {"stat_code": "901Y009", "item_code": "0", "unit": "2020=100"},
}


def fetch_ecos_indicator(indicator_name, months_back=6, cycle="M"):
    api_key = st.secrets.get("ECOS_API_KEY", "")
    if not api_key or indicator_name not in ECOS_INDICATORS:
        return pd.DataFrame()
    info = ECOS_INDICATORS[indicator_name]
    end = datetime.date.today()
    start = end - datetime.timedelta(days=months_back * 31)
    start_str, end_str = start.strftime("%Y%m"), end.strftime("%Y%m")
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/50/"
        f"{info['stat_code']}/{cycle}/{start_str}/{end_str}/{info['item_code']}"
    )
    try:
        r = requests.get(url, timeout=6)
        data = r.json()
        rows = data.get("StatisticSearch", {}).get("row", [])
        df = pd.DataFrame(rows)
        if not df.empty:
            df["지표명"] = indicator_name
            df["단위"] = info["unit"]
        return df
    except Exception:
        return pd.DataFrame()


def ecos_to_text(df, indicator_name):
    if df.empty:
        return ""
    latest = df.iloc[-1]
    unit = latest.get("단위", "")
    return f"- {indicator_name} 최신값({latest.get('TIME','')}): {latest.get('DATA_VALUE','')}{unit}"


def fetch_dart_disclosures(corp_code, max_items=5):
    api_key = st.secrets.get("DART_API_KEY", "")
    if not api_key or not corp_code:
        return pd.DataFrame()
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {"crtfc_key": api_key, "corp_code": corp_code, "page_no": 1, "page_count": max_items}
    try:
        r = requests.get(url, params=params, timeout=6)
        data = r.json()
        return pd.DataFrame(data.get("list", []))
    except Exception:
        return pd.DataFrame()


def dart_to_text(df):
    if df.empty:
        return ""
    lines = [f"- {row.get('corp_name','')}: {row.get('report_nm','')} ({row.get('rcept_dt','')})" for _, row in df.iterrows()]
    return "\n".join(lines)


WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

KST = ZoneInfo("Asia/Seoul")

def get_today_institution():
    today = WEEKDAY_KR[datetime.datetime.now(KST).weekday()]
    for name, info in INSTITUTIONS.items():
        if today in info["days"]:
            return name, info
    name = random.choice(list(INSTITUTIONS.keys()))
    return name, INSTITUTIONS[name]


QUESTION_PROMPT = """
너는 {institution}의 논술 출제위원이다.
아래 [오늘의 시사자료]와 [최신 경제 통계]를 근거로, 실제 {institution} 채용 논술 시험처럼
1) 제시문(600~900자, 통계/기사 인용 포함)
2) 논제(1~2개 질문, 800~1000자 분량 서술 요구)
를 작성하라.

[기관 출제 특성] 주요 출제 영역: {focus} / 출제 스타일: {style}

[오늘의 시사자료]
{articles}

[최신 경제 통계]
{stats}

[기업 공시 참고자료]
{dart}

출력 형식:
[제시문]
...
[논제]
1. ...
2. (선택) ...
"""

HINT_PROMPT = """
아래 논제에 대한 '힌트'를 제공하라. 정답을 직접 쓰지 말고 아래 4가지만 제시:
1. 핵심 키워드 5~7개
2. 관련 심층 기사/보도자료 제목 3개
3. '킥'이 되는 전문 용어 2~3개와 짧은 정의
4. 답안 구조 (서론-본론(2~3개 소주제)-결론) 개요만 bullet로
논제: {question}
"""

ANSWER_PROMPT = """
아래 논제에 대해 {institution} 채용 논술 만점 수준의 모범답안을 작성하라.
- 분량: 1000~1200자, 서론-본론-결론 구조
- 본론은 [현황/원인 분석] -> [기관 특성 연계 시사점] -> [구체적 정책·대안] 순서
- 마지막 "상위권을 가르는 핵심 포인트" 섹션: (a) 가점 표현/논리 (b) 흔한 감점 요인 (c) 차별화 팁
논제: {question} / 기관 특성: {focus}
"""

GRADING_PROMPT = """
너는 {institution} 논술 채점위원이다. 아래 [논제]와 [응시자 답안]을 다음 5가지 기준으로 평가하라.

1. 논리적 구조 및 전개 (논리성): 서론(현황 및 문제 제기)-본론(원인 분석 및 대응 방안)-결론(요약 및 향후 전망)으로 이어지는 3단 구조가 명확한가.
2. 주제 부합도 및 타당성 (적합성): 논제가 묻는 핵심을 정확히 파악하고, 원인과 해결책이 현실적이고 타당한가.
3. 전문 지식 및 객관적 근거 (전문성): 정확한 경제·금융 개념, 정책, 통계, 구체적 사례로 주장을 뒷받침하는가.
4. 시사 통찰력 및 창의성: 현상 나열에 그치지 않고 자신만의 시각, 참신한 대안, 파급효과를 논리적으로 제시하는가.
5. 문장력 및 분량: 맞춤법, 띄어쓰기, 문장 구사력, 요구 분량(2,000자 내외)의 70~80% 이상을 일관성 있게 채웠는가.

[논제]
{question}

[응시자 답안]
{user_answer}

다음 형식으로 간결하게 작성하라:

**총점 및 백분위**: OO점/100 (상위 OO% 추정)

**항목별 점수** (각 20점 만점, 한줄 코멘트):
- 논리성:
- 적합성:
- 전문성:
- 시사통찰력·창의성:
- 문장력·분량:

**장점** (핵심만 2~3개, 답안 문장 인용):
-

**단점** (핵심만 2~3개):
-

**총평**: (한 줄)

**앞으로의 글쓰기 습관 발전 방향**:
위 장단점을 고려해, 이 응시자가 앞으로 논술 연습을 어떻게 하면 자신만의 논술 스타일을 특화시킬 수 있는지
구체적인 습관·훈련 방법을 2~3개 bullet로 제시하라.
"""

CORRECTION_PROMPT = """
너는 {institution} 논술 채점위원이자 첨삭 전문가다.
아래 [응시자 답안]을 다음 원칙에 따라 100점 합격 수준으로 교정하라.

원칙:
1. 응시자의 문체, 어투, 주요 논리 흐름과 표현 스타일은 최대한 유지한다 (완전히 새로 쓰지 않는다).
2. 논리 구조(서론-본론-결론), 근거의 구체성, 전문용어 정확성, 맞춤법/문장력을 100점 수준으로 보완한다.
3. 부족한 통계·사례·정책 근거는 자연스럽게 추가하되, 응시자의 원래 주장 방향은 바꾸지 않는다.
4. 교정 후, 원문 대비 무엇을 어떻게 바꿨는지 3~5개 bullet로 짧게 설명한다.

[논제]
{question}

[응시자 답안]
{user_answer}

출력 형식:
[교정된 답안]
...

[교정 포인트 요약]
- ...
"""


def generate_question(institution, info, articles_text, stats_text, dart_text):
    return call_solar(
        QUESTION_PROMPT.format(
            institution=institution, focus=", ".join(info["focus"]),
            style=info["style"], articles=articles_text,
            stats=stats_text or "(해당 없음)", dart=dart_text or "(해당 없음)",
        ),
        temperature=0.5,
    )


def generate_hint(question):
    return call_solar(HINT_PROMPT.format(question=question), temperature=0.3)


def generate_answer(question, institution, info):
    return call_solar(
        ANSWER_PROMPT.format(question=question, institution=institution, focus=", ".join(info["focus"])),
        temperature=0.3, max_tokens=2000,
    )


def grade_answer(question, user_answer, institution):
    return call_solar(
        GRADING_PROMPT.format(question=question, user_answer=user_answer, institution=institution),
        temperature=0.2, max_tokens=1800,
    )


def correct_answer(question, user_answer, institution):
    return call_solar(
        CORRECTION_PROMPT.format(question=question, user_answer=user_answer, institution=institution),
        temperature=0.3, max_tokens=2200,
    )


def get_gsheets_conn():
    try:
        from streamlit_gsheets import GSheetsConnection
        if "connections" not in st.secrets or "gsheets" not in st.secrets.get("connections", {}):
            return None
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception:
        return None


QUESTION_COLS = ["날짜", "기관", "제시문논제"]
HISTORY_COLS = ["날짜", "시간", "기관", "논제", "내답안", "채점결과", "교정답안"]


def load_questions_sheet():
    conn = get_gsheets_conn()
    if conn is None:
        return pd.DataFrame(columns=QUESTION_COLS)
    try:
        df = conn.read(worksheet="Questions", ttl=0)
        return df.dropna(how="all")
    except Exception:
        return pd.DataFrame(columns=QUESTION_COLS)


def get_or_create_today_question(date_str, institution, generator_fn):
    conn = get_gsheets_conn()
    df = load_questions_sheet()
    if conn is not None and not df.empty:
        match = df[(df["날짜"] == date_str) & (df["기관"] == institution)]
        if not match.empty:
            return match.iloc[-1]["제시문논제"], False
    new_question = generator_fn()
    save_today_question(date_str, institution, new_question)
    return new_question, True


def save_today_question(date_str, institution, question_text):
    conn = get_gsheets_conn()
    if conn is None:
        return
    df = load_questions_sheet()
    df = df[~((df["날짜"] == date_str) & (df["기관"] == institution))]
    new_row = pd.DataFrame([{"날짜": date_str, "기관": institution, "제시문논제": question_text}])
    df = pd.concat([df, new_row], ignore_index=True)
    try:
        conn.update(worksheet="Questions", data=df)
    except Exception as e:
        st.error(f"문제 저장 실패: {e}")


def load_history_sheet():
    conn = get_gsheets_conn()
    if conn is None:
        return pd.DataFrame(columns=HISTORY_COLS)
    try:
        df = conn.read(worksheet="History", ttl=0)
        return df.dropna(how="all")
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLS)


def append_history(date_str, institution, question, user_answer, grading_result="", corrected=""):
    conn = get_gsheets_conn()
    if conn is None:
        return False
    df = load_history_sheet()
    new_row = pd.DataFrame([{
        "날짜": date_str, "시간": datetime.datetime.now().strftime("%H:%M:%S"),
        "기관": institution, "논제": question, "내답안": user_answer,
        "채점결과": grading_result, "교정답안": corrected,
    }])
    df = pd.concat([df, new_row], ignore_index=True)
    try:
        conn.update(worksheet="History", data=df)
        return True
    except Exception as e:
        st.error(f"기록 실패: {e}")
        return False


st.set_page_config(page_title="금융공기업 논술 트레이너", layout="wide")
page = st.sidebar.radio("메뉴", ["오늘의 논술 연습", "나의 논술 히스토리"])
today_str = datetime.date.today(KST).date().isoformat()

if page == "오늘의 논술 연습":
    st.title("금융공기업·은행 논술 트레이너")
    inst_name, inst_info = get_today_institution()
    st.subheader(f"오늘의 출제 기관: {inst_name}")
    st.caption(f"출제 스타일: {inst_info['style']} | 오늘 날짜: {today_str}")

    if "df_news" not in st.session_state:
        with st.spinner("오늘의 시사자료 수집 중 (기관 공식 RSS)..."):
            st.session_state.df_news = fetch_daily_articles()

    with st.expander("오늘의 시사자료 (기관 공식 RSS 자동 수집)"):
        st.dataframe(st.session_state.df_news, use_container_width=True)

    with st.expander("한국은행 ECOS 통계 (선택, API 키 필요)"):
        ecos_dfs = []
        for ind in inst_info["ecos_indicators"]:
            df_ind = fetch_ecos_indicator(ind)
            if not df_ind.empty:
                st.write(f"**{ind}**")
                st.dataframe(df_ind[["TIME", "DATA_VALUE"]].tail(6), use_container_width=True)
                ecos_dfs.append(ecos_to_text(df_ind, ind))
        st.session_state.stats_text = "\n".join(ecos_dfs)
        if not ecos_dfs:
            st.caption("ECOS_API_KEY가 없거나 해당 기관에 매핑된 지표가 없습니다.")

    with st.expander("금융감독원 OpenDART 공시정보 (선택, API 키 + 기업코드 필요)"):
        corp_code = st.text_input("DART 기업 고유번호 (8자리, 예: 삼성전자 00126380)", key="corp_code")
        if corp_code:
            df_dart = fetch_dart_disclosures(corp_code)
            st.dataframe(df_dart, use_container_width=True)
            st.session_state.dart_text = dart_to_text(df_dart)
        else:
            st.session_state.dart_text = ""

    st.markdown("---")

    def _generate_new_question():
        articles_text = "\n".join(
            f"- {r['출처']}: {r['제목']}" for _, r in st.session_state.df_news.iterrows()
        )
        return generate_question(
            inst_name, inst_info, articles_text,
            st.session_state.get("stats_text", ""), st.session_state.get("dart_text", ""),
        )

    if "question_full" not in st.session_state or st.session_state.get("question_date") != today_str:
        with st.spinner("오늘의 문제를 불러오는 중..."):
            q_text, is_new = get_or_create_today_question(today_str, inst_name, _generate_new_question)
        st.session_state.question_full = q_text
        st.session_state.question_date = today_str
        for k in ["hint", "answer", "grading", "corrected"]:
            st.session_state[k] = None
        if is_new:
            st.info("오늘의 새 문제가 생성되었습니다.")
        else:
            st.info("오늘 이미 생성된 문제를 불러왔습니다. (같은 날에는 문제가 고정됩니다)")

    colA, colB = st.columns([3, 1])
    with colB:
        if st.button("새로 생성하기 (오늘 문제 교체)"):
            with st.spinner("새로운 문제를 생성하는 중..."):
                new_q = _generate_new_question()
            save_today_question(today_str, inst_name, new_q)
            st.session_state.question_full = new_q
            st.session_state.question_date = today_str
            for k in ["hint", "answer", "grading", "corrected"]:
                st.session_state[k] = None
            st.success("오늘의 문제가 새로 교체되었습니다.")
            st.rerun()

    st.markdown("### 오늘의 제시문 & 논제")
    st.write(st.session_state.question_full)

    user_answer = st.text_area("답안을 작성해보세요 (권장 2,000자 내외)", height=350, key="user_answer")
    st.caption(f"현재 글자 수: {len(user_answer)}자")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("힌트 보기"):
            with st.spinner("힌트 생성 중..."):
                st.session_state.hint = generate_hint(st.session_state.question_full)
    with col2:
        if st.button("모범답안 보기"):
            with st.spinner("모범답안 생성 중..."):
                st.session_state.answer = generate_answer(st.session_state.question_full, inst_name, inst_info)
    with col3:
        if st.button("내 답안 채점받기"):
            if not user_answer.strip():
                st.warning("답안을 먼저 작성해주세요.")
            else:
                with st.spinner("채점 중..."):
                    st.session_state.grading = grade_answer(st.session_state.question_full, user_answer, inst_name)
                append_history(today_str, inst_name, st.session_state.question_full, user_answer,
                                grading_result=st.session_state.grading, corrected=st.session_state.get("corrected", ""))
                st.toast("채점 결과가 히스토리에 자동 기록되었습니다.")
    with col4:
        if st.button("내 답안 교정받기"):
            if not user_answer.strip():
                st.warning("답안을 먼저 작성해주세요.")
            else:
                with st.spinner("필체를 유지하며 100점 수준으로 교정 중..."):
                    st.session_state.corrected = correct_answer(st.session_state.question_full, user_answer, inst_name)
                append_history(today_str, inst_name, st.session_state.question_full, user_answer,
                                grading_result=st.session_state.get("grading", ""), corrected=st.session_state.corrected)
                st.toast("교정 결과가 히스토리에 자동 기록되었습니다.")

    if st.session_state.get("hint"):
        st.info(st.session_state.hint)
    if st.session_state.get("answer"):
        st.success(st.session_state.answer)
    if st.session_state.get("grading"):
        st.markdown("### 채점 결과 (AI 채점위원)")
        st.warning(st.session_state.grading)
    if st.session_state.get("corrected"):
        st.markdown("### 교정된 100점 합격 답안 (내 필체 유지)")
        st.info(st.session_state.corrected)

    if get_gsheets_conn() is None:
        st.caption("참고: Google Sheets가 연동되어 있지 않아 하루 고정/자동 기록 기능이 비활성화된 상태입니다. secrets.toml의 [connections.gsheets]를 설정하면 활성화됩니다.")

else:
    st.title("나의 논술 히스토리 (Google Sheets 연동)")
    df_hist = load_history_sheet()
    if df_hist.empty:
        st.info("아직 저장된 기록이 없거나 Google Sheets가 연동되어 있지 않습니다.")
    else:
        dates = sorted(df_hist["날짜"].dropna().unique(), reverse=True)
        selected_date = st.selectbox("날짜 선택", dates)
        day_rows = df_hist[df_hist["날짜"] == selected_date]
        for _, row in day_rows.iterrows():
            with st.expander(f"{row['날짜']} {row.get('시간','')} | {row['기관']}"):
                st.markdown("**논제**")
                st.write(row["논제"])
                st.markdown("**내가 쓴 답안**")
                st.write(row["내답안"])
                if row.get("채점결과"):
                    st.markdown("**채점 결과**")
                    st.write(row["채점결과"])
                if row.get("교정답안"):
                    st.markdown("**교정된 답안**")
                    st.write(row["교정답안"])
        st.download_button(
            "전체 히스토리 CSV 다운로드",
            df_hist.to_csv(index=False).encode("utf-8-sig"),
            file_name="essay_history.csv",
        )
