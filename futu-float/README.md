# 富途牛牛 單一個股懸浮視窗

一個永遠置頂、無邊框、可拖曳的小視窗，透過 Futu OpenAPI 即時顯示一檔股票的報價。

```
US.AAPL                ●
蘋果
227.48
+2.31   +1.03%
盤後 227.90  +0.18%
H 228.10  L 224.92  V 41.23M
更新 15:59:58
```

## 安裝

1. 下載並開啟 **OpenD**（富途官方的行情閘道），用富途牛牛帳號登入。
   預設監聽 `127.0.0.1:11111`。
2. 安裝 Python 3.8 以上（Windows / macOS 官方安裝檔都已內含 tkinter），再安裝套件：

   ```
   pip install -r requirements.txt
   ```

## 使用

```
python floating_quote.py                 # 用上次的股票（第一次預設 US.BE（Bloom Energy））
python floating_quote.py --code TSLA     # 指定股票
python floating_quote.py --demo          # 不連 OpenD，用假資料先看外觀
python floating_quote.py --host 192.168.1.10 --port 11111   # OpenD 在別台電腦
```

| 操作 | 功能 |
| --- | --- |
| 左鍵拖曳 | 移動視窗（位置會記住） |
| 雙擊 | 換股票 |
| 右鍵（macOS 也可 Control+點擊） | 選單：換股票、紅漲綠跌、精簡模式、永遠置頂、透明度、結束 |

代碼可以簡寫：`AAPL` → `US.AAPL`、`700` → `HK.00700`、`600519` → `SH.600519`、`000001` → `SZ.000001`。

設定（股票、位置、透明度…）存在同資料夾的 `floating_quote.json`。

## 說明

- 美股會顯示盤前／盤後／夜盤價（有資料時）。
- 價格變動時背景會閃一下紅／綠。
- OpenD 沒開時，視窗會顯示「連線 OpenD …」並持續重試，開啟後自動連上；斷線後也會自動重連並重新訂閱。
- 需要該市場的行情權限，沒有權限時狀態列會顯示訂閱失敗的原因。
- Futu OpenAPI 不提供台股行情，所以台股無法使用。
