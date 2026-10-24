# online_reputation

상장법인 네이버 뉴스 평판 분석기

1) 상장법인목록.xls에서 (회사명, 종목코드) 읽기

모든 상장법인의 종목 코드가 저장된 파일을 순차적으로 읽어 회사명과 종목코드를 추출

2) 네이버 뉴스 검색으로 오늘 날짜 기사 URL 수집 후 본문 추출

네이버는 한번 검색에 100개의 기사 URL만 허용한다.  BeautifulSoup package를 이용해서 본문 text만 추출.

3) llama.cpp (port 8000) EXAONE-3.5-32B로 감성 분석 (긍정/부정/중립)

ollama보다 메모리를 적게 사용하는 llama-server를 이용하여 한글 처리에 유리한 EXAONE-3.5를 실행 

```
llama-server \
    -m EXAONE-3.5-32B-Instruct-Q4_K_M.gguf \
    -ngl 999 -cb \
    -c 8192 -n 8192 \
    --temp 1.0 --top-p 0.95 --min-p 0 \
    --port 8000 \
    -a EXAONE-3.5-32B
```


4) chase-<date>.csv에 결과 저장

파이썬 thread를 이용하여 3개의 thread가 동시에 네이버 뉴스 검색, 기사 평판을 별도로 실행해서 자원을 사용을 효율화. 

평판을 얻기 위한 프롬프트는 다음과 같음. 

```
    prompt = (
        f"다음은 '{company}' 회사에 관한 뉴스 기사입니다.\n"
        f"이 기사가 해당 회사에 긍정적인지, 부정적인지, 중립적인지 판단하세요.\n"
        f"반드시 아래 세 단어 중 하나만 답하세요: 긍정, 부정, 중립\n\n"
        f"제목: {title}\n"
        f"내용: {body}\n\n"
        f"답변:"
    )
```

# 환경
llama-server  8753 (5ba14d568)
python 3.12.3
LLM model EXAONE-3.5-32B-Instruct-Q4_K_M.gguf
RTX 3090
