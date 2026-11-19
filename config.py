import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "leads.db")
TIKHUB_BASE = os.environ.get("TIKHUB_BASE", "https://api.tikhub.io").rstrip("/")

# Defaults written to the settings table on first run; edit them on the 设置 page.
DEFAULT_SETTINGS = {
    # 数据接口 Key 不随源码提供：用环境变量 TIKHUB_API_KEY 首次写入，或登录后在「管理后台 → 设置」里填写
    "api_key": os.environ.get("TIKHUB_API_KEY", ""),
    # 接口地址：留空 = 直连 TikHub；拿到的是中转子 Key（hk_ 开头）时，填提供者给你的网关地址
    "api_base": "",
    "intent_strong": "\n".join([
        "想了解", "了解一下", "怎么联系", "联系方式", "怎么合作", "想合作", "求合作",
        "想加盟", "怎么加盟", "加盟费", "想投资", "怎么投资", "想做", "我也想",
        "怎么买", "哪里买", "在哪买", "想买", "求链接", "求购", "怎么报名",
        "感兴趣", "有兴趣", "加微信", "电话多少", "怎么找你", "私聊", "带带我",
    ]),
    "intent_medium": "\n".join([
        "多少钱", "价格", "报价", "费用", "怎么收费", "贵不贵", "成本", "回本", "利润",
        "赚钱吗", "能赚", "靠谱吗", "靠谱", "效果怎么样", "好用吗", "售后",
        "在哪", "哪里有", "怎么弄", "怎么做", "有没有", "考虑", "咨询", "私信",
    ]),
    "intent_exclude": "\n".join([
        "私信我", "加我", "找我", "滴滴我", "厂家直销", "我们公司", "招代理", "诚招",
    ]),
    # 计费：1 积分 = 1 元。每个作品按实际抓到的评论数，每 50 条为一档收费。
    "price_post": "0.3",
    "price_post_wechat": "1.0",
    # 对标账号：每刷新一次（自动或手动）扣的积分
    "price_track": "0.1",
    # AI 仿写用的是客户自己的阿里云 Key（填在各自的「我的账户」里），这里只放默认模型名。
    "ai_text_model": "qwen-plus",
    "ai_image_model": "qwen-image-2.0",
    "ai_video_model": "wan2.7",
    # open = 注册即可用；approval = 注册后需管理员审核；closed = 关闭注册
    "register_mode": "approval",
    "register_bonus": "0",
    # 充值套餐：每行「付款金额 赠送积分」，用户在充值页选择；收款码图片存在 media/pay-*.png|jpg
    "recharge_packages": "50 0\n200 20\n500 80\n1000 200",
    "recharge_contact": "",   # 联系方式（微信号等），显示在充值页
    "recharge_note": "",      # 充值页补充说明
}

COMMENTS_PER_TIER = 50
