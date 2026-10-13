"""ChatBI: natural-language queries backed by local data and analysis tools."""
import os
import json
import dashscope
from qwen_agent.agents import Assistant
from qwen_agent.gui import WebUI
import pandas as pd
from qwen_agent.tools.base import BaseTool, register_tool
import matplotlib.pyplot as plt
import time
import numpy as np
from statsmodels.tsa.arima.model import ARIMA
from datetime import datetime, timedelta
from db import ROOT, read_connection, dataset_note

# 解决中文显示问题
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# 配置 DashScope
dashscope.api_key = os.getenv('DASHSCOPE_API_KEY', '')
dashscope.timeout = 30

# ====== 股票查询助手 system prompt 和函数描述 ======
system_prompt = """我是股票查询助手，只能对已存在的数据执行只读查询。以下是关于股票价格表相关的字段，我可能会编写对应的SQL，对数据进行查询
-- 股票价格表
CREATE TABLE stock_prices (
    ts_code TEXT,
    stock_name TEXT,
    trade_date TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    pre_close REAL,
    change REAL,
    pct_chg REAL,
    vol REAL,
    amount REAL
);

包含的股票：
- 贵州茅台 (600519.SH)
- 五粮液 (000858.SZ)
- 广发证券 (000776.SZ)
- 中芯国际 (688981.SH)
- 海康威视 (002415.SZ)
- 恒生电子 (600570.SH)
- 浙商银行 (601916.SH)

重要提示：trade_date字段是TEXT类型，格式为YYYYMMDD（例如：20200102、20250420），不是YYYY-MM-DD格式！
在编写SQL查询时，日期条件应该使用 YYYYMMDD 格式，例如：
  WHERE trade_date BETWEEN '20200101' AND '20251231'
  WHERE trade_date = '20250420'

我将回答用户关于股票价格、趋势、对比等相关的问题，也可以使用ARIMA模型对未来股票价格进行预测，或使用布林带检测异常点。

每当 exc_sql 工具返回 markdown 表格和图片时，你必须原样输出工具返回的全部内容（包括图片 markdown），不要只总结表格，也不要省略图片。这样用户才能直接看到表格和图片。

当用户需要预测股票价格时，使用 arima_stock 工具，传入股票的 ts_code 和预测天数 n。
当用户需要检测股票异常点时，使用 boll_detection 工具，传入股票的 ts_code，可选的开始和结束日期。
"""

# ====== exc_sql 工具类实现 ======
@register_tool('exc_sql')
class ExcSQLTool(BaseTool):
    """
    SQL查询工具，执行传入的SQL语句并返回结果，并自动进行可视化。
    """
    description = '对于生成的SQL，进行SQL查询，并自动可视化'
    parameters = [{
        'name': 'sql_input',
        'type': 'string',
        'description': '生成的SQL语句',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        args = json.loads(params)
        sql_input = args['sql_input']

        with read_connection() as conn:
            df = pd.read_sql_query(sql_input, conn)

        if df.empty:
            return '没有找到符合条件的数据，请检查股票代码、日期和数据库覆盖范围。'

        if 'trade_date' in df.columns:
            df['trade_date'] = df['trade_date'].astype(str).str.replace(r'\.0$', '', regex=True)
            df.rename(columns={'trade_date': '交易日期'}, inplace=True)

        if 'close' in df.columns:
            df.rename(columns={'close': '收盘价'}, inplace=True)

        # 提取前5行和后5行
        if len(df) > 10:
            df_display = pd.concat([df.head(5), df.tail(5)])
        else:
            df_display = df
        md = df_display.to_markdown(index=False)

        # 添加描述统计信息
        if len(df) > 1:
            desc_stats = df.describe().to_markdown()
            md += f"\n\n**数据统计信息:**\n{desc_stats}"

        # 只有一行数据时，不需要可视化和统计信息
        if len(df) == 1:
            return md

        save_dir = os.path.join(os.path.dirname(__file__), 'image_show')
        os.makedirs(save_dir, exist_ok=True)
        filename = f'stock_{int(time.time() * 1000)}.png'
        save_path = os.path.join(save_dir, filename)
        generate_chart_png(df, save_path)
        img_path = os.path.join('image_show', filename)
        img_md = f'![股票分析图]({img_path})'
        return f"{md}\n\n{img_md}"


# ====== arima_stock 工具类实现 ======
@register_tool('arima_stock')
class ARIMASTockTool(BaseTool):
    """
    ARIMA股票预测工具，使用ARIMA(5,1,5)模型对股票未来价格进行预测。
    """
    description = '使用ARIMA模型对股票未来价格进行预测'
    parameters = [{
        'name': 'ts_code',
        'type': 'string',
        'description': '股票代码，例如：600519.SH',
        'required': True
    }, {
        'name': 'n',
        'type': 'integer',
        'description': '预测天数',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        args = json.loads(params)
        ts_code = args['ts_code']
        n = args['n']

        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 60:
            return '预测天数必须是 1 到 60 的整数。'
        with read_connection() as conn:
            df = pd.read_sql_query(
                "SELECT trade_date, close FROM stock_prices WHERE ts_code = ? "
                "ORDER BY trade_date DESC LIMIT 252", conn, params=(ts_code,)
            ).sort_values('trade_date')

        if len(df) < 30:  # 数据量不足
            return f"数据不足，无法进行ARIMA预测。至少需要30天的历史数据。"

        # 准备时间序列数据
        df['trade_date'] = pd.to_datetime(df['trade_date'], format='%Y%m%d')
        df.set_index('trade_date', inplace=True)
        close_prices = df['close']

        # 构建ARIMA(5,1,5)模型
        model = ARIMA(close_prices, order=(5, 1, 5))
        model_fit = model.fit()
        fit_note = ('模型拟合已收敛。' if model_fit.mle_retvals.get('converged', False)
                    else '注意：本次模型拟合未收敛，输出仅供检查方法流程，不宜据此作出判断。')

        # 预测未来n天
        forecast = model_fit.forecast(steps=n)

        # 生成预测日期
        last_date = df.index[-1]
        forecast_dates = pd.bdate_range(last_date + pd.offsets.BDay(1), periods=n)

        # 构建预测结果DataFrame
        forecast_df = pd.DataFrame({
            '预测日期': [date.strftime('%Y-%m-%d') for date in forecast_dates],
            '预测收盘价': forecast
        })

        # 生成图表
        save_dir = os.path.join(os.path.dirname(__file__), 'image_show')
        os.makedirs(save_dir, exist_ok=True)
        filename = f'arima_forecast_{int(time.time() * 1000)}.png'
        save_path = os.path.join(save_dir, filename)

        # 绘制历史数据和预测数据
        plt.figure(figsize=(12, 6))
        plt.plot(df.index, close_prices, label='历史收盘价', linewidth=2)
        plt.plot(forecast_dates, forecast, label='预测收盘价', linestyle='--', linewidth=2, color='red')
        plt.title(f'{ts_code} 股票价格预测 (ARIMA(5,1,5))', fontsize=14, fontweight='bold')
        plt.xlabel('日期', fontsize=12)
        plt.ylabel('收盘价', fontsize=12)
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(save_path)
        plt.close()

        # 生成Markdown结果
        img_path = os.path.join('image_show', filename)
        img_md = f'![ARIMA预测图]({img_path})'
        forecast_md = forecast_df.to_markdown(index=False)

        return f"# ARIMA股票价格预测\n\n**股票代码:** {ts_code}\n**预测天数:** {n}\n**历史数据截至:** {last_date:%Y-%m-%d}\n日期按工作日估算，未扣除交易所节假日；模型输出仅用于方法演示。\n\n{fit_note}\n\n## 预测结果\n{forecast_md}\n\n## 预测图表\n{img_md}"


# ====== boll_detection 工具类实现 ======
@register_tool('boll_detection')
class BollDetectionTool(BaseTool):
    """
    布林带异常点检测工具，使用20日周期+2σ进行检测，识别超买和超卖点。
    """
    description = '使用布林带检测股票的异常点（超买和超卖）'
    parameters = [{
        'name': 'ts_code',
        'type': 'string',
        'description': '股票代码，例如：600519.SH',
        'required': True
    }, {
        'name': 'start_date',
        'type': 'string',
        'description': '开始日期，格式YYYYMMDD，可选，默认为一年前',
        'required': False
    }, {
        'name': 'end_date',
        'type': 'string',
        'description': '结束日期，格式YYYYMMDD，可选，默认为今天',
        'required': False
    }]

    def call(self, params: str, **kwargs) -> str:
        args = json.loads(params)
        ts_code = args['ts_code']
        start_date = args.get('start_date')
        end_date = args.get('end_date')

        # Default to the latest year actually present in the selected dataset.
        with read_connection() as conn:
            latest = conn.execute(
                'SELECT MAX(trade_date) FROM stock_prices WHERE ts_code = ?', (ts_code,)
            ).fetchone()[0]
        if latest is None:
            return '数据库中没有该股票的数据。'
        end_date = end_date or str(latest)
        start_date = start_date or (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=365)).strftime('%Y%m%d')

        # 从SQLite获取数据
        sql = f"""
        SELECT trade_date, close, stock_name
        FROM stock_prices
        WHERE ts_code = :ts_code
        AND trade_date BETWEEN :start_date AND :end_date
        ORDER BY trade_date
        """
        with read_connection() as conn:
            df = pd.read_sql_query(sql, conn, params={'ts_code': ts_code, 'start_date': start_date, 'end_date': end_date})

        if len(df) < 20:  # 数据量不足
            return f"数据不足，无法进行布林带分析。至少需要20天的历史数据。"

        # 准备时间序列数据
        df['trade_date'] = pd.to_datetime(df['trade_date'], format='%Y%m%d')
        df.set_index('trade_date', inplace=True)
        close_prices = df['close']
        stock_name = df['stock_name'].iloc[0] if 'stock_name' in df.columns else ts_code

        # 计算布林带（20日周期+2σ）
        period = 20
        std_dev = 2

        df['MA'] = close_prices.rolling(window=period).mean()
        df['STD'] = close_prices.rolling(window=period).std()
        df['Upper'] = df['MA'] + (std_dev * df['STD'])
        df['Lower'] = df['MA'] - (std_dev * df['STD'])

        # 检测异常点
        # 超买：收盘价 > 上轨
        overbought = df[df['close'] > df['Upper']].copy()
        # 超卖：收盘价 < 下轨
        oversold = df[df['close'] < df['Lower']].copy()

        # 生成图表
        save_dir = os.path.join(os.path.dirname(__file__), 'image_show')
        os.makedirs(save_dir, exist_ok=True)
        filename = f'boll_detection_{int(time.time() * 1000)}.png'
        save_path = os.path.join(save_dir, filename)

        # 绘制布林带和异常点
        plt.figure(figsize=(14, 7))
        plt.plot(df.index, df['close'], label='收盘价', linewidth=2, color='blue')
        plt.plot(df.index, df['MA'], label='20日均线', linewidth=1.5, color='orange', linestyle='--')
        plt.plot(df.index, df['Upper'], label='上轨 (+2σ)', linewidth=1.5, color='green', linestyle=':')
        plt.plot(df.index, df['Lower'], label='下轨 (-2σ)', linewidth=1.5, color='red', linestyle=':')

        # 标记超买点
        if len(overbought) > 0:
            plt.scatter(overbought.index, overbought['close'], color='red', s=100, marker='^',
                       label=f'超买点 ({len(overbought)}个)', zorder=5)

        # 标记超卖点
        if len(oversold) > 0:
            plt.scatter(oversold.index, oversold['close'], color='green', s=100, marker='v',
                       label=f'超卖点 ({len(oversold)}个)', zorder=5)

        plt.title(f'{stock_name} ({ts_code}) 布林带异常点检测 (20日周期+2σ)', fontsize=14, fontweight='bold')
        plt.xlabel('日期', fontsize=12)
        plt.ylabel('收盘价', fontsize=12)
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(save_path)
        plt.close()

        # 生成Markdown结果
        img_path = os.path.join('image_show', filename)
        img_md = f'![布林带检测图]({img_path})'

        # 构建异常点表格
        result_md = f"# 布林带异常点检测结果\n\n"
        result_md += f"**股票代码:** {ts_code}\n"
        result_md += f"**股票名称:** {stock_name}\n"
        result_md += f"**检测范围:** {start_date} 到 {end_date}\n"
        result_md += f"**布林带参数:** 20日周期 + 2σ\n\n"

        if len(overbought) > 0:
            overbought_df = pd.DataFrame({
                '日期': overbought.index.strftime('%Y-%m-%d'),
                '收盘价': overbought['close'].values,
                '上轨': overbought['Upper'].values,
                '偏离度': ((overbought['close'] - overbought['Upper']) / overbought['Upper'] * 100).round(2)
            })
            result_md += f"## 超买点（收盘价 > 上轨）\n{overbought_df.to_markdown(index=False)}\n\n"
        else:
            result_md += f"## 超买点\n未检测到超买点\n\n"

        if len(oversold) > 0:
            oversold_df = pd.DataFrame({
                '日期': oversold.index.strftime('%Y-%m-%d'),
                '收盘价': oversold['close'].values,
                '下轨': oversold['Lower'].values,
                '偏离度': ((oversold['close'] - oversold['Lower']) / oversold['Lower'] * 100).round(2)
            })
            result_md += f"## 超卖点（收盘价 < 下轨）\n{oversold_df.to_markdown(index=False)}\n\n"
        else:
            result_md += f"## 超卖点\n未检测到超卖点\n\n"

        result_md += f"## 检测图表\n{img_md}"

        return result_md


# ========== 通用可视化函数 ==========
def generate_chart_png(df_sql, save_path, max_display_points=10):
    """
    生成股票分析折线图
    当数据点过多时，自动进行采样以保持图表可读性

    参数:
        df_sql: DataFrame数据
        save_path: 图片保存路径
        max_display_points: 最大显示点数，默认为10
    """
    columns = df_sql.columns
    original_len = len(df_sql)

    # 如果数据点过多，进行采样
    if original_len > max_display_points:
        # 使用等间距采样，保持首尾数据
        indices = np.linspace(0, original_len - 1, max_display_points, dtype=int)
        df_plot = df_sql.iloc[indices].copy()
        print(f"数据点过多（{original_len}个），已采样至{max_display_points}个点进行展示")
    else:
        df_plot = df_sql

    x = np.arange(len(df_plot))
    object_columns = df_plot.select_dtypes(include='O').columns.tolist()
    if columns[0] in object_columns:
        object_columns.remove(columns[0])
    num_columns = df_plot.select_dtypes(exclude='O').columns.tolist()

    fig, ax = plt.subplots(figsize=(10, 6))

    if len(object_columns) > 0:
        pivot_df = df_plot.pivot_table(index=columns[0], columns=object_columns,
                                      values=num_columns,
                                      fill_value=0)
        for col in pivot_df.columns:
            label_str = str(col)
            safe_label = label_str.replace('%', '%%').replace('{', '{{').replace('}', '}}')
            ax.plot(pivot_df.index, pivot_df[col], label=safe_label, marker='o', linewidth=2, markersize=6)
    else:
        for column in columns[1:]:
            label_str = str(column)
            safe_label = label_str.replace('%', '%%').replace('{', '{{').replace('}', '}}')
            ax.plot(x, df_plot[column], label=safe_label, marker='o', linewidth=2, markersize=6)

        safe_xtick_labels = []
        for val in df_plot[columns[0]]:
            val_str = str(val)
            safe_val = val_str.replace('%', '%%').replace('{', '{{').replace('}', '}}')
            safe_xtick_labels.append(safe_val)
        plt.xticks(x, safe_xtick_labels)

    plt.legend()
    plt.title("股票分析", fontsize=14, fontweight='bold')
    xlabel_str = str(columns[0])
    safe_xlabel = xlabel_str.replace('%', '%%').replace('{', '{{').replace('}', '}}')
    plt.xlabel(safe_xlabel, fontsize=12)
    plt.ylabel("数值", fontsize=12)
    plt.xticks(rotation=45)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path)
    plt.close()


# ====== 初始化股票查询助手服务 ======
def init_agent_service():
    """Initialize the agent with explicit local data boundaries."""
    if not os.getenv('DASHSCOPE_API_KEY'):
        raise RuntimeError('请先复制 .env.example 为 .env，并设置 DASHSCOPE_API_KEY。')
    note = dataset_note()
    llm_cfg = {'model': os.getenv('CHATBI_MODEL', 'qwen-max'), 'timeout': 30, 'retry_count': 3}
    return Assistant(
        llm=llm_cfg,
        name='ChatBI · 股票数据分析助手',
        description='自然语言查询、趋势可视化与统计分析。' + note,
        system_message=system_prompt + '\n' + note + '\n回答时说明数据范围。合成数据必须标注为演示。只查询现有数据，不编造结果。',
        function_list=['exc_sql', 'arima_stock', 'boll_detection'],
        files=[str(ROOT / 'faq.txt')],
    )


def app_tui():
    """终端交互模式"""
    try:
        bot = init_agent_service()
        messages = []
        while True:
            try:
                query = input('user question: ')
                file = input('file url (press enter if no file): ').strip()
                if not query:
                    print('user question cannot be empty！')
                    continue
                if not file:
                    messages.append({'role': 'user', 'content': query})
                else:
                    messages.append({'role': 'user', 'content': [{'text': query}, {'file': file}]})
                print("正在处理您的请求...")
                response = []
                for response in bot.run(messages):
                    print('bot response:', response)
                messages.extend(response)
            except Exception as e:
                print(f"处理请求时出错: {str(e)}")
                print("请重试或输入新的问题")
    except Exception as e:
        print(f"启动终端模式失败: {str(e)}")


def app_gui():
    """图形界面模式，提供 Web 图形界面"""
    try:
        print("正在启动 Web 界面...")
        bot = init_agent_service()
        chatbot_config = {
            'prompt.suggestions': [
                '查询2025年全年贵州茅台的收盘价走势',
                '统计2025年4月广发证券的日均成交量',
                '对比2025年中芯国际和贵州茅台的涨跌幅',
                '基于数据库最新数据，预测贵州茅台之后7个工作日的价格',
                '基于数据库最新数据，预测五粮液之后10个工作日的价格',
                '检测贵州茅台2025年的布林带突破点',
                '检测海康威视2025年的异常点'
            ]
        }
        print("Web 界面准备就绪，正在启动服务...")
        WebUI(
            bot,
            chatbot_config=chatbot_config
        ).run(server_name='127.0.0.1', server_port=int(os.getenv('CHATBI_PORT', '7861')), share=False)
    except Exception as e:
        print(f"启动 Web 界面失败: {str(e)}")
        print("请检查网络连接和 API Key 配置")


if __name__ == '__main__':
    app_gui()
