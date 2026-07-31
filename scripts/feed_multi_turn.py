# -*- coding: utf-8 -*-
"""
多轮对话灌数据脚本 - 测试长期记忆功能
每个 session 包含 3-8 轮真实对话，模拟完整客服交互流程
"""
import sys, os, json, time, psycopg
sys.stdout.reconfigure(encoding='utf-8')
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base)

os.environ['DATABASE_URL'] = 'postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph'
DSN = os.environ['DATABASE_URL']
conn = psycopg.connect(DSN, autocommit=True)
USER_ID = "zwy"

# ============================================================
# 多轮对话场景（每个场景是一个完整的客服对话流程）
# ============================================================

MULTI_TURN_SCENARIOS = [
    # --- 场景1: X-100 智能音箱咨询（提到产品名 -> 触发产品兴趣记忆）---
    {
        "name": "x100_speaker_inquiry",
        "turns": [
            ("你好，我想了解下你们X-100智能音箱的功能", "chat",
             "您好！很高兴为您介绍X-100智能音箱。这款产品支持语音助手、在线音乐播放、智能家居控制、闹钟提醒等功能。您对哪方面比较感兴趣？"),
            ("语音助手支持哪些功能？能控制家电吗？", "consult",
             "X-100的语音助手支持播放音乐、查询天气、设置闹钟、语音搜索等。同时它也支持智能家居控制，兼容市面上主流的智能灯、空调、电视等设备，您可以通过语音指令控制开关和调节。需要我详细介绍接入方法吗？"),
            ("那空调控制具体怎么操作？我家是格力空调", "consult",
             "格力空调是可以的！步骤如下：1) 打开智联APP，进入智能家居页面；2) 添加设备，选择空调品牌「格力」；3) 按照提示完成Wi-Fi配对；4) 配对成功后就可以用语音说「小智小智，打开客厅空调」来控制了。整个过程大约5分钟就能完成。"),
            ("好的谢谢，那音质怎么样？低音效果好吗", "consult",
             "X-100采用了2.25英寸全频扬声器+独立低音振膜的设计，低音表现相当不错。频响范围在60Hz-20kHz，最大输出功率15W。如果您对音质要求比较高，我们还有X-300 Pro型号，配备了双扬声器和独立低音炮，音质会更上一层楼。"),
            ("X-300 Pro多少钱？比X-100贵多少", "consult",
             "X-100售价299元，X-300 Pro售价699元。X-300 Pro多了独立低音炮、支持Dolby Atmos音效、还带有一个LED触控屏显示信息。性价比来说X-100已经很棒了，如果您是发烧友建议上X-300 Pro。"),
            ("好的我再考虑考虑，谢谢你的详细介绍", "chat",
             "不客气！您随时可以来找我咨询。如果您决定购买，可以告诉我，我帮您查看最新优惠活动。祝您生活愉快！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景2: 产品问题投诉+升级（测试 unresolved 记忆）---
    {
        "name": "complaint_unresolved",
        "turns": [
            ("我买的X-100用了不到一周就连不上Wi-Fi了", "complaint",
             "非常抱歉听到这个问题！连不上Wi-Fi是比较常见的情况，我先帮您排查一下：1) 请确认Wi-Fi密码是否正确；2) 尝试重启音箱（拔掉电源10秒再插上）；3) 检查路由器是否开启了MAC地址过滤。您试试看行不行？"),
            ("这些方法都试过了还是不行，Wi-Fi列表都搜不到", "complaint",
             "明白了，搜不到Wi-Fi列表说明问题比较严重，可能是Wi-Fi模块故障。我帮您申请换新处理。请问您的订单号是多少？我马上帮您走换新流程。"),
            ("订单号是 ZD20260729001，急着用能不能加急", "complaint",
             "已查到您的订单！Z-100于7月22日购买。我为您申请了加急换新，今天内发出，预计明天送达。新的音箱发货后我会短信通知您。非常抱歉给您带来不便！"),
            ("好的那旧音箱怎么处理？里面的数据呢", "complaint",
             "旧音箱您寄回即可，我们会提供顺丰上门取件，运费我们承担。至于数据，X-100的数据是云端同步的，换新后登录同一个账号就能恢复您的设置和偏好。稍后我会把退货指引和运单号发给您。"),
            ("那寄回去之后大概多久能收到新的？", "complaint",
             "我们已经安排了加急发货，新品预计明天下午送到。您今天寄回旧品即可。如果有任何问题随时联系我，我会一直跟进直到您收到新品为止。"),
            ("好的希望这次没问题了，之前的体验真的很差", "chat",
             "我完全理解您的心情。我已经在系统里标记了您的设备需要特别关注，新设备发货前会经过严格质检。这是我的工号ZWY001，有任何问题直接找我。再次为给您带来的不便致歉！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景3: 用户提供个人信息（测试 save_profile 记忆）---
    {
        "name": "user_profile_setup",
        "turns": [
            ("你好，我是新用户，想了解一下你们的产品", "chat",
             "您好！欢迎来到智联科技！我帮您创建一个用户档案，方便以后更好地为您服务。请问您怎么称呼？有什么方面的产品感兴趣呢？"),
            ("我叫李明，对智能音箱和智能家居比较感兴趣", "chat",
             "好的李先生，已为您记录！智能音箱和智能家居是我们非常成熟的产品线。X系列智能音箱销量超过100万台，支持语音控制500+品牌的家电。您家里目前有哪些智能设备呢？"),
            ("家里有个小米的智能灯和格力空调，想知道能不能一起控制", "consult",
             "可以的！X-100智能音箱支持对接米家生态和格力智能家居系统。您只需要在APP里依次添加设备即可，操作非常简单。所有设备添加好后，一句话就能同时控制灯光和空调。"),
            ("那这个APP支持iOS和安卓都能用吗", "consult",
             "支持的！智联APP在iOS App Store和各大安卓应用商店都可以免费下载。支持iOS 14+和Android 10+。您可以先用手机下载注册，添加设备的过程我随时指导您。"),
            ("好的我下载了，注册完了然后呢", "consult",
             "好的，打开APP后点击右下角的「添加设备」，选择「智能音箱」-「X-100」，然后按住音箱顶部的配对键3秒听到提示音后，APP会自动搜索到设备。跟着屏幕提示完成Wi-Fi连接就搞定了。有什么问题随时问我。"),
            ("连上了！确实挺简单的，谢谢你指导", "chat",
             "太好了！很高兴能帮到您。李先生，您的用户档案已创建完毕。如果您以后有任何问题，直接说您是李先生，我就能调取您的设备信息快速帮您处理。祝您使用愉快！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景4: 多次购买行为（测试 session 持续性和兴趣叠加）---
    {
        "name": "repeat_customer",
        "turns": [
            ("我之前买过X-100，想再买一个送朋友", "consult",
             "感谢您再次光临！您是老用户了，老朋友可以享受老用户专享价哦。X-100原价299元，老用户回购价只需269元。购买时输入老用户专享码「VIP269」即可享受优惠。需要现在下单吗？"),
            ("好的，那能帮我看看上次买的订单方便参考下配置吗", "consult",
             "好的，我查一下您的历史订单。您上次买的是白色款X-100，配置是标准版。您这次想买同款还是换个颜色？我们还有黑色、蓝色和粉色可选。礼盒装的话粉色很受欢迎。"),
            ("送男性朋友，黑色吧，再加个礼盒包装", "consult",
             "黑色礼盒装是个好选择！我帮您下单：X-100黑色版+精美礼盒包装，总价289元（礼盒包装费20元）。考虑到您是第二次购买，我为您申请免邮费。预计明天发货，后天可以到。确认下单吗？"),
            ("确认，谢谢！对了上次买的那个用的挺好的", "chat",
             "感谢您的支持！听您说用得好我就放心了。已经为您下单成功，订单号ZD20260729002。礼盒包装我们会在发货前拍照确认。您朋友收到后如果有什么问题，可以让他直接找我说是您的朋友就行。"),
            ("好的太棒了，那以后买配件也能找你吗", "chat",
             "当然可以！您是我们的老客户了，以后有任何需求直接找我就好。配件购买、产品咨询、售后问题，我都会优先为您处理。已保存您为VIP客户。祝您朋友喜欢这份礼物！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景5: 复杂售后（退货+换货+退款组合，多步骤）---
    {
        "name": "complex_aftersale",
        "turns": [
            ("你好，我上周买了两台X-100，有一台开机有异响", "complaint",
             "您好，很抱歉听到这个问题！有异响的话可能是扬声器振膜有问题。您方便拍一段异响的视频吗？我帮您判断一下是硬件故障还是正常现象。"),
            ("拍了视频发不了这里，但很明显是不正常的滋滋声", "complaint",
             "了解了，滋滋声确实不正常。建议方案：有异响的那台我帮您换新，另外一台如果您也想退的话可以一起处理。您是想只换那一台，还是两台都退？"),
            ("好的的那台留着，有问题的换新。能顺便把另一台的发票也一起开吗", "complaint",
             "完全可以。那台好的我帮您把发票电子版发到邮箱。换新的那台我今天安排发出，明天到货。旧的您寄回，我们出运费。请问您的邮箱是？"),
            ("邮箱是 liming@email.com，谢谢", "chat",
             "好的，已记录。发票会在24小时内发到您的邮箱。换新订单已生成，旧品上门取件师傅会明天和您联系。两台设备的处理我都标记好了，您放心。"),
            ("那保修的期限是怎么算的？换新的重新算吗", "consult",
             "是的！换新的那台保修期重新计算，从您收到新品之日起享受1年质保。好的那台按原购买日期算。整个过程我会跟进，有任何问题直接找我。"),
            ("明白了，处理得很清楚，谢谢你！", "chat",
             "不客气！感谢您的耐心配合。祝您使用愉快，有问题随时找我！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景6: 多语言/跨文化场景 ---
    {
        "name": "multilingual",
        "turns": [
            ("Hello, I bought an X-100 speaker but can't set it up. Can you help?", "consult",
             "Hello! Of course, I'd be happy to help you set up your X-100 speaker. First, please make sure you have downloaded the ZHI-LINK app from the App Store. Have you already installed it?"),
            ("Yes, I installed it but when I scan the QR code it says device not found", "consult",
             "I see. Let me help you troubleshoot: 1) Make sure your phone is connected to the 2.4GHz Wi-Fi network (the X-100 doesn't support 5GHz). 2) Press and hold the Bluetooth/Wi-Fi button on the back for 5 seconds until the LED blinks rapidly. 3) Try scanning the QR code again."),
            ("Oh I was on 5GHz! Let me switch and try again", "consult",
             "Yes, that's a common mistake! The X-100 only supports 2.4GHz networks. Once you switch, press and hold the pairing button again to enter pairing mode, then try scanning the QR code. Let me know how it goes!"),
            ("It worked! Thank you so much for your help!", "chat",
             "You're welcome! I'm glad it worked. If you need any further assistance with your X-100, such as connecting smart home devices or setting up voice commands, please don't hesitate to reach out. Have a great day! 🎵"),
            ("Can I switch the voice assistant to English?", "consult",
             "Yes! You can switch the voice assistant language in the app settings. Go to: Device Settings → Voice Assistant → Language → Select 'English (US)'. After switching, you can use voice commands in English to control your speaker."),
            ("Perfect, all set now. Thanks again!", "chat",
             "You're very welcome! Enjoy your X-100 speaker experience. If you have friends who might be interested, feel free to refer them. Have a great day! 😊"),
        ],
        "expected_resolved": True,
    },
    # --- 场景7: 情绪化客户+持续跟踪 ---
    {
        "name": "emotional_care",
        "turns": [
            ("我实在是受不了了，这个音箱买了三个月坏了三次，你们到底行不行", "complaint",
             "听到您这么说我心里也很难过。三个月坏了三次，这确实不应该。我非常理解您的不满和失望。请您给我一个机会，我一定会帮您彻底解决这个问题的。方便告诉我您的订单号吗？"),
            ("订单号ZD20260415003，你们自己看吧", "complaint",
             "已查到您的记录。您的前两次维修记录我也看到了——第一次换主板，第二次换Wi-Fi模块。这个情况说明这台设备可能存在设计缺陷。我建议这次不再维修，直接为您换一台全新的设备，并且将保修期延长一年。您看这样可以吗？"),
            ("换新的话能保证这次不会坏吗？我真的不想再折腾了", "complaint",
             "我完全理解您的心情。我向您保证：1) 换新的设备我们会做全面质检，确保出厂零缺陷；2) 这次换新后保修期延长至两年；3) 我会在系统里标记您的账户，如果再有类似问题将直接升级给客服总监处理。这是我能给到的最强保障了。"),
            ("好吧，那就换新吧，希望这次靠谱", "chat",
             "谢谢您愿意再给我们一次机会。我已经安排了今日发货，新品明天到达。旧品我们上门取件。我已经把您的案例标记为最高优先级，我会亲自跟踪直到您确认新品使用正常。有任何问题微信直找我。"),
            ("好的，谢谢你。其实我之前生气也是因为真的很喜欢这个产品", "chat",
             "您这句话让我心里暖暖的。正是因为有您这样热爱我们产品的用户，我们才有动力不断改进。这次换新后有啥问题直接找我，不管是白天晚上，我看到就会回复。"),
            ("好的，那就这样吧，等明天收到新品", "chat",
             "好的，明天收到新品后我建议您先用两天看看效果。第三天我会主动联系您做回访。祝您今晚睡个好觉，明天新音箱就到了！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景8: 账号+多产品交叉咨询 ---
    {
        "name": "cross_product_account",
        "turns": [
            ("你好，我之前用手机号138xxxx注册的账号忘了密码", "consult",
             "您好！密码找回很简单。请打开智联APP，在登录页面点击「忘记密码」，输入您注册的手机号，接收验证码后即可重置密码。如果您注册手机号已经停用，我可以帮您通过邮箱验证的方式找回。"),
            ("手机号还能收验证码，我试一下", "consult",
             "好的，您先尝试。如果收不到验证码，可以检查一下是否被手机自动归类为垃圾短信。一般1分钟内会收到，稍等一下试试看。"),
            ("收到了，已经重置成功登录了！对了，我之前买过X-100，现在想看看你们有没有新的配件", "consult",
             "欢迎回来！我看到您之前购买过X-100（白色款，4月购买）。我们最近出了几个新配件：1) X-100专用防尘罩（29元）；2) 桌面支架（39元，可调节角度）；3) 户外便携电池底座（99元，可续航12小时）。您对哪个感兴趣？"),
            ("桌面支架看起来不错，能调节高度吗？", "consult",
             "桌面支架支持高度调节（5-15cm）和角度调节（0-30度），底部有防滑垫，使用很稳固。我建议搭配X-100使用效果很好，还能改善音质因为减少了桌面共振。需要帮您下单吗？"),
            ("好的来一个吧，还有其他新品也推荐下", "consult",
             "好的，已为您添加桌面支架到购物车。另外我们本周新上了X-200智能闹钟（可语音控制，带温湿度检测和助眠白噪音），新品价149元。还有Type-C充电底座（支持X-100和X-200），49元。您看看感兴趣的？"),
        ],
        "expected_resolved": True,
    },
    # --- 场景9: 不同心情时段跟踪（跨时段记忆）---
    {
        "name": "time_crossing_service",
        "turns": [
            ("你好，我的X-200今天早上突然没声音了，但是灯是亮着的", "complaint",
             "您好！灯亮但没声音，这种情况可能是音量被误触调到了最小，或者音频输出切换到了蓝牙模式。您先尝试：按「音量+」键调大音量，或者检查一下是否连接了蓝牙设备导致扬声器静音。"),
            ("哦还真是，连了蓝牙耳机没注意，断掉就好了！谢谢", "chat",
             "不客气！这种情况挺常见的，连了蓝牙后扬声器会自动静音。如果您平时经常需要切换蓝牙设备，建议在设置里关闭「自动切换」功能，这样就可以手动选择音频输出了。"),
            ("好的我关掉自动切换，对了还有个问题——上次说的桌面支架到货了吗", "consult",
             "我查一下您的订单... 桌面支架昨天已到当地配送站，预计今天下午送达。您可以留意一下快递电话。收到后如有安装问题随时找我，安装很简单，卡扣式设计无需工具。"),
            ("好的好的，你们效率不错！", "chat",
             "谢谢夸奖！我争取一直保持这个效率 😊 以后有任何问题，直接找我就行。祝您今天愉快！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景10: 售后满意度回访（模拟真实回访流程）---
    {
        "name": "satisfaction_followup",
        "turns": [
            ("你好，我是上次换新的用户，你说今天会联系我的", "chat",
             "李先生您好！没错，我特意安排了今天的回访。您的新X-100收到了吗？用得怎么样？"),
            ("收到了！比预期的还早到，昨天就到了。目前用着挺好的", "chat",
             "太好了！听到这个消息我放心多了。新设备没有出现之前的异响问题吧？连接Wi-Fi和蓝牙都正常吗？"),
            ("都正常，声音也很清楚。不过想问一下，这个新设备固件需要更新吗", "consult",
             "出厂固件是最新版本v2.3.1，暂时不需要更新。但如果您开启了自动更新，有新版本时会自动推送。您可以在APP里「设备信息」页面查看固件版本，如果后续有更新建议您及时升级，会修复一些小问题并增加新功能。"),
            ("好的，那之前的旧设备你们收到了吗？退款大概什么时候到", "consult",
             "旧设备昨天已签收，质检通过后就会处理退款。一般在签收后3-5个工作日内原路返回。您是用支付宝支付的，会退到支付宝账户。我会关注退款进度，到账后通知您。"),
            ("好的，服务很好，比以前满意多了", "chat",
             "非常感谢您的肯定！我会继续保持。以后有任何问题随时找我，我的工号ZWY001。您的满意是我们最大的动力！"),
        ],
        "expected_resolved": True,
    },
    # --- 场景11: 企业客户技术咨询 ---
    {
        "name": "business_tech_support",
        "turns": [
            ("你好，我是公司IT负责人，想批量采购50台X-300 Pro作为办公设备", "consult",
             "您好！感谢您的批量采购意向！X-300 Pro确实非常适合办公场景。作为企业用户，我们可以提供：1) 批量采购折扣（50台可享85折）；2) 集中部署服务；3) 企业级售后支持。我帮您联系大客户经理详细对接，您方便留个联系方式吗？"),
            ("电话13812345678，邮箱 it@company.com。除了设备之外，你们有没有统一管理平台？", "consult",
             "有的！我们的「智联企业云管家」平台支持批量管理所有设备状态、固件升级、配置下发和故障监控。特别适合您这种50台以上的部署规模。我来安排产品工程师给您做一个线上演示，大约30分钟。本周四下午您方便吗？"),
            ("周四下午2点可以，主要想看管理平台的功能", "consult",
             "好的，已安排本周四下午2点的线上演示。工程师会通过邮件把会议链接发给您。同时我会先把企业采购方案和报价单发给您参考。您方便收邮件的邮箱就是刚才那个 it@company.com 吗？"),
            ("对，就那个邮箱。报价单麻烦尽快", "consult",
             "好的，报价单一小时内发到您邮箱。内容包括：设备单价（企业价）、总价和折扣明细、部署服务费、首年管理平台费用。另外我们为企业客户提供30天试用，不满意可退。您放心决策。"),
            ("很好，效率不错。那周四见", "chat",
             "感谢您的信任！我已经把您的需求记录到CRM系统，我、大客户经理和产品工程师都会参与跟进。周四见！期待与您的合作。"),
        ],
        "expected_resolved": True,
    },
    # --- 场景12: 多语言混合+海外用户 ---
    {
        "name": "overseas_user",
        "turns": [
            ("你好，我在美国买了X-100带回国用，电压会不会有问题", "consult",
             "您好！X-100的电源适配器支持100-240V宽电压输入，在美国110V和国内220V都可以使用。只需要换一个电源插头转换器就可以了，不需要变压器。"),
            ("美版和国版功能上有什么区别吗？语音助手能识别中文吗", "consult",
             "硬件上是完全一样的。美版出厂默认为英语，但您可以在设置里切换为中文。切换到中文后语音助手就能正常识别中文指令了。不过美版预装的应用商店是Google Play，国内使用可能需要网络配置。"),
            ("那保修在国内能用吗？需要转保吗", "consult",
             "美版产品在中国大陆不享受官方保修。但是！我可以协助您处理——您可以将设备寄到我们深圳的服务中心，我们可以提供付费维修服务。如果您经常往返中美，建议在国内再买一台国行版，这样保修更方便。"),
            ("好的，我知道了。那在美国买的应用和国内通用吗", "consult",
             "应用账号是通用的，您在APP里登录同一个账号，美版购买的内容和配置会自动同步。不过部分区域限制的流媒体服务（如某些国内音乐平台）需要切换应用商店区域才能使用。总体来说带回国用没问题。"),
            ("谢谢你的详细解答，很专业！", "chat",
             "不客气！如果后续在使用过程中遇到任何问题，随时联系我。祝您回国顺利，使用愉快！"),
        ],
        "expected_resolved": True,
    },
]

# ============================================================
# 写入数据库
# ============================================================

def write_scenario(scenario, base_idx):
    """写入一个多轮对话场景到数据库"""
    name = scenario['name']
    turns = scenario['turns']
    session_id = "zwy_mt_%04d" % base_idx
    
    done = 0
    now = __import__('datetime').datetime.now().isoformat()
    
    for turn_idx, (user_msg, intent, bot_reply) in enumerate(turns):
        # Save conversation
        conn.execute("""
            INSERT INTO conversation_history
            (user_id, session_id, user_message, bot_reply, intent, emotion,
             emotion_intensity, resolved, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            USER_ID, session_id, user_msg, bot_reply, intent,
            'negative' if intent == 'complaint' else 'positive' if intent == 'chat' else 'neutral',
            3 if intent == 'complaint' else 1,
            scenario['expected_resolved'] and turn_idx == len(turns) - 1,
            now
        ))
        
        # Update session
        conn.execute("""
            INSERT INTO sessions
            (session_id, user_id, title, created_at, last_active, message_count)
            VALUES (%s, %s, %s, %s, %s, 1)
            ON CONFLICT(session_id) DO UPDATE SET
              title=CASE WHEN sessions.title IS NULL OR sessions.title=''
                         THEN EXCLUDED.title ELSE sessions.title END,
              last_active=EXCLUDED.last_active,
              message_count=sessions.message_count+1
        """, (session_id, USER_ID, turns[0][0][:80], now, now))
        
        # Update product interests (extracts product names from user messages)
        # This is key for memory testing!
        from agent.memory import _update_product_interests
        _update_product_interests(session_id, user_msg, user_id=USER_ID)
        
        done += 1
    
    return session_id, done


def main():
    t0 = time.time()
    total_turns = 0
    total_sessions = 0
    
    # Check before
    before = conn.execute(
        "SELECT COUNT(*) FROM conversation_history WHERE user_id='zwy'"
    ).fetchone()[0]
    print("Before: %d records in conversation_history" % before)
    
    print("\n=== 写入 %d 个多轮对话场景 ===\n" % len(MULTI_TURN_SCENARIOS))
    
    for i, scenario in enumerate(MULTI_TURN_SCENARIOS):
        session_id, turns = write_scenario(scenario, i + 1)
        total_turns += turns
        total_sessions += 1
        name = scenario['name']
        print("  [%d/%d] %s: %d turns -> %s" % (
            i+1, len(MULTI_TURN_SCENARIOS), name, turns, session_id))
    
    elapsed = time.time() - t0
    
    # Check after
    after = conn.execute(
        "SELECT COUNT(*) FROM conversation_history WHERE user_id='zwy'"
    ).fetchone()[0]
    session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id='zwy'"
    ).fetchone()[0]
    
    # Check memory context
    from agent.memory import build_memory_context as bmc
    ctx = bmc(USER_ID)
    
    print("\n=== 结果 ===")
    print("  新写入: %d 轮对话, %d 个 session" % (total_turns, total_sessions))
    print("  conversation_history: %d -> %d (+%d)" % (before, after, after - before))
    print("  总 sessions: %d" % session_count)
    print("  耗时: %.1fs" % elapsed)
    
    # Show memory context
    print("\n=== build_memory_context('zwy') ===")
    if ctx:
        # Clean up encoding for display
        print(ctx[:800])
    else:
        print("(empty)")
    
    # Show product interests
    print("\n=== user_preferences product_interests ===")
    prefs = conn.execute(
        "SELECT DISTINCT product_interests FROM user_preferences WHERE user_id='zwy'"
    ).fetchall()
    seen = set()
    for p in prefs:
        interests = json.loads(p['product_interests']) if isinstance(p['product_interests'], str) else p['product_interests']
        for item in interests:
            if item not in seen:
                print("  - %s" % item)
                seen.add(item)
    
    conn.close()

import json
main()
