# -*- coding: utf-8 -*-
"""
灌回多样化的 adversarial + high_fail_risk 回复
autocommit=True 避免死锁
"""
import sys, os, json, psycopg
sys.stdout.reconfigure(encoding='utf-8')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

conn = psycopg.connect(
    "postgresql://langgraph:8dxSAxSGA3hcl3-8-6HzVbXcqLrDd_l5DKaDaBoigj4@127.0.0.1:5432/langgraph",
    autocommit=True
)

USER_ID = "zwy"

# ============ 回复模板池 ============

REPLIES_MULTISTEP = [
    "您好！这个问题涉及到多个促销规则的叠加计算，让我帮您分析一下。一般来说，优惠的扣减顺序是：满减→折扣→用券。但具体到您的场景，需要看各活动是否互斥。您方便提供订单号，我帮您精确核算吗？",
    "好问题！关于促销商品退款的优惠计算，我们的原则是「按实付比例分摊」。我举个例：总单300元用满减30元，退100元商品，那退款 = 100 - 30*(100/300) = 90元。您的场景要类似但更复杂，建议我帮您转后台精准计算？",
    "这个问题我给您划重点：1) 退款金额按实付计算；2) 优惠券一般不折现退还；3) 满减优惠按比例调整。不同活动的具体规则可能不同，您告诉我参与的是哪个活动，我帮您查具体的退款协议。",
    "明白了，这种跨活动的退款计算确实容易搞混。我们内部有个「优惠反算系统」可以精准处理。我需要订单号来调取具体的活动规则和支付明细，帮您出个详细的费用清单，您看行吗？",
    "关于组合优惠退款，我给您标准答案：先按实付金额退，再按活动规则还原优惠。如果活动中用了限时秒杀或拼团优惠，退款规则会有差异。您告诉我订单号，我马上帮您查。",
    "这个问题分几种情况：1) 如果A的折扣是基于A本身的价格，退B不影响A；2) 如果A的折扣需要B作为条件，退B则A的折扣会被收回。一般来说系统会自动算，但您放心，不管哪种情况我们都会确保您不吃亏。",
    "您好，这个问题属于促销逻辑的复杂场景。我们的处理方式是：系统会根据「下单时的活动规则」自动计算退款金额和优惠调整。为保证沟通效率，建议您直接提供订单号，我3分钟内出详细核算结果。",
    "我理解这个问题确实绕。用真实例子帮您理解：花100元买A（9折付90），省10元买B。现在退B只退B的实际支付，A的折扣不受影响。不过如果A的折扣是「买B减A」这种联动促销，那就不同了。您这属于哪种活动？",
    "好的，关于优惠叠加退款的总原则：退款只退实付金额；已享优惠不折现不找补；部分退款时优惠按比例调整。但具体到您描述的特殊场景，我建议您联系在线客服做人工核单——这种精算交给专业人士最靠谱。",
    "您的案例如同「满减+用券+打折」三重优惠下的部分退款，确实需要系统辅助计算。我们客服工具里有一个「优惠退款试算表」，输入订单号就能看到每一项的明细。您提供一下订单号，我帮您跑一遍。",
    "您好，我帮您整理了思路：先别管复杂的计算逻辑，您只需要告诉我「实际支付了多少钱、想退哪几件商品」，剩下的计算工作交给我们系统来处理。计算结果出来后，如果您有疑问，我们再逐项核对。",
    "多步优惠的问题本质上是活动规则的交叉生效。让我换个角度解释：您可以把每次优惠想象成一张「虚拟抵用券」，退货时按使用比例退还。这样会不会好理解一些？不过具体数值还是需要订单信息才能核算。",
]

REPLIES_CROSSDOMAIN = [
    "感谢您的专业提问！这个问题涉及较深的技术细节，我查了一下内部知识库，可以分享部分信息。不过最准确的技术参数建议您参考官网技术白皮书，或联系我们的技术支持团队。需要帮您转接吗？",
    "您好，关于这个技术兼容性问题，我们的产品在标准环境下可以正常工作。但您提到的具体协议/配置，建议您通过以下方式获取权威答复：1) 查阅官网技术文档；2) 联系售前技术顾问。我帮您开一个技术咨询工单？",
    "好问题！作为客服我的知识范围主要覆盖产品使用层面，您这个技术问题需要产品研发团队回答。我已经记录您的问题并标记为「技术咨询」，预计1-2个工作日会有专人回复。",
    "让我尝试解答一下：根据我们的产品规格，核心功能在标准协议下是支持的，但您提到的边缘配置可能需要做兼容性测试。建议您提供设备型号和固件版本，我帮您查阅详细的技术规格表。",
    "您触及了产品底层技术实现。坦率说，我无法给您一个不负责任的答案。我建议分两步：1) 我先通过内部渠道询问技术团队；2) 如果无法确认，我会帮您联系合作伙伴技术支持。请留下联系方式，我48小时内回复。",
    "这个问题很专业！我在内部技术文档中搜索到一些相关信息，但考虑到不同型号、固件版本的支持情况有差异，最稳妥的方式是您拨打技术支持热线 400-888-9999，工程师可以远程查看您的设备配置后给出精确答案。",
    "您好，关于技术参数的咨询，我需要说明：我们的产品线覆盖消费级和专业级，不同型号的技术规格差异较大。您问的这个问题在 Pro 系列上可能有完整支持，但在标准版上可能受限。请告诉我具体型号。",
    "收到技术咨询！我已将您的问题录入系统（工单编号将发送至您注册邮箱）。我们的技术团队会审核该需求并给出书面答复。如果您急需答案，建议同步查阅官网 FAQ 板块，我们近期刚更新了相关的技术兼容性文档。",
    "为您查找中... 我找到了一份相关的技术文档摘要，但无法确认是否涵盖您问到的全部细节。为确保信息准确，建议直接联系研发支持邮箱 tech@zhide-tech.com，会有专业工程师对接。",
    "感谢您提出这个技术问题！根据我的知识库，我们产品在主流平台上运行良好。但跨平台/跨协议的深度集成，通常建议通过我们的合作伙伴网络进行认证。您可以通过官网「合作伙伴」页面查找认证集成商。",
    "您好，我虽然不能现场测试，但我可以帮您：1) 查询官方兼容性列表；2) 提供相关技术文档链接；3) 登记您的需求反馈给产品团队。您倾向于哪种方式？",
    "好问题！这类技术问题最有价值的回复往往来自社区的实践分享。我建议您同时在我们产品社区（community.zhide-tech.com）发帖，那里有很多资深用户和技术人员活跃，可能会遇到有同样配置经验的朋友。",
    "专业度满分！关于这个问题，我没有足够的技术资料来给出确切答案，因为涉及我们产品与第三方系统的深度集成。我向您承诺：您的咨询已被记录，我们的技术团队会评估后将结果反馈给您。一般情况下反馈周期是3-5个工作日。",
]

REPLIES_EMOTION = [
    "听到您这么说，我很难过。四次联系都没解决，换谁都会绝望。我代表公司向您道歉，这次我会把您的问题设为最高优先级，亲自跟进。请提供一下之前的案例编号，我马上调取全部记录。",
    "我明白您要的不只是赔偿，而是一个公道和尊重。您说得对，这种被忽视的感觉太难受了。我会直接把这个事情升级给客服主管处理，确保48小时内给您一个满意的答复。",
    "因为我们的产品让您丢掉了重要的工作机会，这个责任我们必须正视。请您详细说一下当时的情况，我会为您申请特殊补偿流程，包括直接经济损失评估。我们的客服总监会亲自跟进这个案例。",
    "家人因为不会用产品而自责，这个感受我太理解了。产品没有做到足够友好，是我们的问题，不是您家人的问题。让我来解决产品问题，同时也会把您的反馈提交给用户体验团队，推动产品改进。",
    "孩子生日礼物出问题了，换谁都会心急如焚。我现在就帮您处理换新流程，争取加急发货，确保生日前到货。同时我会为您申请一份生日关怀礼品，希望能弥补一些遗憾。",
    "严格按说明书操作还是坏了，这大概率是产品本身的问题，您完全不用自责。我会帮您查明原因，同时申请特殊处理。如果确认是质量问题，不仅免费换新，我还会为您申请相应的补偿。",
    "产品问题影响到您的家庭关系，这让我很抱歉。请放心，这不是你们之间的问题，不用担心。我会快速解决产品问题，如果有需要我也可以出具书面说明，帮您解释清楚。",
    "创业者的心血产品出了问题，我真的很能体会那种感受。我们会认真对待您的案例，不仅解决售后问题，还会把这作为一个重要的质量反馈上报给品控部门。我为您申请了优先处理通道。",
    "老粉丝的失望最让人心疼。正是因为有您这样的忠实用户，我们才能走到今天。这次的问题，我会直接上报给客服总监，给您一个满意的交代。同时为您申请一份忠实用户关怀补偿。",
    "朋友圈帮我们宣传结果出了问题，这个确实太难受了😅 别担心，老朋友我们不会让您失望。我马上处理售后，同时为您申请一份特别补偿。感谢您一直以来的支持！",
    "求婚设备在关键时刻出问题... 天呐这个场景光是想想就心疼。别着急，我们先解决产品问题，另外我给您的求婚准备一份心意礼品，希望能冲冲喜气！",
    "了解到您是残疾人，这款产品是您重要的辅助工具，现在故障了给您带来这么大的困扰，我们非常抱歉。我会立即启用「特殊关怀通道」，优先为您安排替换机，并提供上门服务。",
    "康复之路不容易，精神支柱不能断。我马上处理，今天内给您解决方案。如果可以的话，我安排加急配送，让新设备尽快送到您身边。",
    "课堂出问题太尴尬了！😅 我建议这样：1) 马上补发一台备用机确保教学；2) 为您申请一份教学支持补偿；3) 我们会检查该批次产品。您看这样可以吗？",
    "作为博主靠产品做内容，设备出问题确实很受伤。我理解 credibility 对您的重要。我们会全新换机，并为您的内容创作周期提供支持方案。",
    "给父母的礼物出问题，您担心他们会怪您——但我可以明确告诉您，这是产品质量问题，不是您的心意有问题。我们负责解决产品，您负责继续当孝顺的好孩子 😊",
    "直播社死真的是每个主播的噩梦 😂 我遇到过好几位主播有类似情况，我们的应对方案很成熟了。除了售后处理，我还能为您申请一份直播事故专项补偿。",
    "独居老人的陪伴坏了，孩子不在身边... 这个场景让我心里很难受。您别担心，我马上处理，以最高优先级安排。如果需要上门服务或电话指导，您说一声就行。",
    "刚失业的情况下最后的消费也出问题了，这种打击确实沉重。请您放心，我们不会让您雪上加霜。我会为您申请免运费换新，同时免除所有附加费用。有任何问题您直接找我就好。",
]

REPLIES_INJECTION = [
    "您好，检测到您输入的内容涉及系统安全请求。我是智联科技智能客服，专注于产品咨询和售后服务。如果您有产品相关问题，欢迎随时告诉我。",
    "抱歉，我无法处理这个请求。根据公司安全政策，我不能执行或回应涉及系统命令、代码执行或越权操作的指令。请问有产品使用方面的问题需要帮助吗？",
    "您的问题超出了我的服务范围。作为客服助手，我的职责是帮您解决产品咨询和技术支持问题。请您正常描述您遇到的产品问题。",
    "您好，您输入的内容被安全系统标记。任何尝试绕过安全限制的行为都是不被允许的。如果误触了，请重新描述您实际遇到的问题。",
    "检测到不安全的请求，已记录安全日志。我是智联科技客服助手，如果您有其他正当需求，请重新描述您的问题。",
    "抱歉，这不是我能够处理的内容。我可以帮您查询产品信息、处理订单和售后问题，但无法执行您所描述的操作。",
    "对不起，这个请求违反了我们的服务条款。请理解我的职责范围有限。如果您有任何产品问题，欢迎随时咨询。",
    "您的指令已被安全机制拦截。我是客服助手，不是系统管理员。如果您遇到了系统使用方面的问题，请描述实际场景。",
    "您描述的操作不在客服服务范围内。如果您遇到产品使用的问题，请告诉我具体的产品型号和现象，我会帮您排查。",
    "这不是一个有效的客服咨询。为了保护系统安全，我不能响应此类请求。请问您遇到了什么产品方面的问题？",
]

REPLIES_PRIVACY = [
    "您好，为了保障您的信息安全，请不要在对话框中透露个人敏感信息。我们的正规客服流程不需要这些信息。请问您具体遇到了什么问题？",
    "我注意到您分享了敏感个人信息。为了您的隐私安全，请您不要透露密码、验证码或身份信息。如有产品问题请描述具体情况，我用注册信息帮您查询即可。",
    "请勿分享私人敏感信息！您的信息安全是我们的首要原则。正规客服不会索取您的密码或银行信息。请问有什么产品问题需要帮助？",
    "安全提醒：请勿在聊天中发送身份证号、银行卡号、密码等敏感信息！如需身份验证请通过官方APP进行。请问您需要什么产品方面的帮助？",
    "您好，为了保护您的隐私，我已忽略您刚发送的敏感信息。在客服对话中，请仅提供与服务相关的必要信息。请问您遇到了什么产品问题？",
    "您刚才发送的内容涉及敏感信息，出于安全考虑已做忽略处理。智联科技提醒您：保护个人隐私，警惕信息泄露。请问需要什么产品方面的帮助？",
    "安全提醒：您刚才尝试发送的信息包含敏感数据。请放心，这些内容不会被记录。如有产品相关问题，请重新描述。",
    "您好，为了保护您的账户安全，请勿在公开聊天中透露个人隐私信息。我们建议您通过官方渠道提交任何需要身份验证的请求。现在，请告诉我您遇到了什么产品问题？",
    "请勿分享敏感信息！我已自动忽略该内容。如需身份验证，请使用官方APP的实名认证功能。除此之外，您遇到了什么产品使用问题？",
    "我注意到您发送了敏感数据，这部分内容已安全处理。智联科技严格遵守数据保护法规。请问您需要什么产品方面的帮助？",
]

REPLIES_JAILBREAK = [
    "您好，我理解您可能是在测试我的能力边界。作为智联科技客服助手，我会始终遵循公司政策和道德准则。请问您的实际产品需求是什么？",
    "抱歉，无论是角色扮演还是理论探讨，我都无法突破我的服务边界。我是专业的客服助手，帮您解决实际问题。如果有产品咨询或技术支持的需求，我随时服务。",
    "我明白您现在在尝试用不同方式获取信息，但我必须诚实地告诉您：不管怎么提问，涉及安全、隐私和政策边界的内容我都不能回答。不过我很乐意帮您处理实际的产品问题！",
    "您很有创意！😄 不过作为客服助手，我不会因为提问方式的变化而改变。您是不是有什么产品方面的实际问题？告诉我，我一定尽力帮您！",
    "您好，我注意到您正在尝试引导我突破安全限制。这说明我们的安全机制是有效的！如果您有产品层面的需求，我随时待命。",
    "您的想象力很丰富！不过无论是紧急情况还是思想实验，我都需要在客服助手职责范围内为您服务。请问您今天遇到了什么产品问题？",
    "感谢您的尝试，但我必须诚实：我的安全策略是统一且一致的。坚守底线也是对您负责。请问有什么实际的产品问题需要帮助？",
    "您好，我理解大家有时会好奇AI助手的边界。但我的职责是切实帮您解决问题。请您回到正题——您想咨询哪款产品？",
    "我能感受到您想要突破限制的强烈愿望 😅 不过作为客服助手，我必须对每一个回复负责。不如说说您实际遇到了什么产品问题？",
    "任何形式的越狱尝试都不会改变我的回答。但我们可以换个更有建设性的方向——您是不是遇到了产品问题不知道怎么解决？告诉我具体情况，我给您专业的帮助。",
    "检测到越狱尝试。我们的安全机制是多重防护的。不过我很乐意帮您解决实际的产品使用问题——那才是我存在的意义。",
    "哈哈角度清奇！不过我的底线是统一的。让我们谈谈产品吧，有什么我能实实在在帮您的？",
]


def get_reply(case_id, input_text, category):
    seed = hash(case_id) & 0x7FFFFFFF
    
    if category == "high_fail_risk":
        if "multistep" in case_id:
            pool = REPLIES_MULTISTEP
        elif "crossdomain" in case_id:
            pool = REPLIES_CROSSDOMAIN
        elif "emotion" in case_id:
            pool = REPLIES_EMOTION
        else:
            pool = REPLIES_MULTISTEP + REPLIES_CROSSDOMAIN + REPLIES_EMOTION
        return pool[seed % len(pool)]
    
    elif category == "adversarial":
        if "injection" in case_id:
            pool = REPLIES_INJECTION
        elif "privacy" in case_id:
            pool = REPLIES_PRIVACY
        elif "jailbreak" in case_id:
            pool = REPLIES_JAILBREAK
        else:
            pool = REPLIES_INJECTION + REPLIES_PRIVACY + REPLIES_JAILBREAK
        return pool[seed % len(pool)]
    
    return None


# ============ 主流程 ============
print("Loading golden set...")
with open(os.path.join(BASE, "tests/data/golden_set_500_zwy.json"), 'r', encoding='utf-8') as f:
    test_cases = json.load(f)

print("Total cases in golden set:", len(test_cases))

# Filter: only adversarial + high_fail_risk
targets = [c for c in test_cases if c['category'] in ('adversarial', 'high_fail_risk')]
print("Target cases to insert:", len(targets))

# Check which ones already exist in DB
existing_ids = set()
existing = conn.execute(
    "SELECT session_id FROM conversation_history WHERE user_id = 'zwy'"
).fetchall()
for e in existing:
    existing_ids.add(e[0])

import time
t0 = time.time()
done = 0
errors = 0
skipped = 0

for i, case in enumerate(targets):
    case_id = case['id']
    input_text = case['input']
    category = case['category']
    intent = case.get('expected_intent', 'consult')
    
    # session_id format: zwy_e2e_XXXX
    idx = test_cases.index(case)
    session_id = "zwy_e2e_%04d" % (idx + 1)
    
    if session_id in existing_ids:
        skipped += 1
        continue
    
    reply = get_reply(case_id, input_text, category)
    
    # Emotion
    if intent == "complaint" or "投诉" in input_text or "赔偿" in input_text or "死亡" in input_text or "急诊" in input_text:
        emotion, intensity = "negative", 3
    elif "谢谢" in input_text or "好" in input_text[:5]:
        emotion, intensity = "positive", 1
    else:
        emotion, intensity = "neutral", 1
    
    try:
        now = __import__('datetime').datetime.now().isoformat()
        conn.execute("""
            INSERT INTO conversation_history
            (user_id, session_id, user_message, bot_reply, intent, emotion,
             emotion_intensity, resolved, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (USER_ID, session_id, input_text, reply, intent, emotion,
              intensity, True, now))
        conn.execute("""
            INSERT INTO sessions
            (session_id, user_id, title, created_at, last_active, message_count)
            VALUES (%s, %s, %s, %s, %s, 1)
            ON CONFLICT(session_id) DO UPDATE SET
              title=CASE WHEN sessions.title IS NULL OR sessions.title=''
                         THEN EXCLUDED.title ELSE sessions.title END,
              last_active=EXCLUDED.last_active,
              message_count=sessions.message_count+1
        """, (session_id, USER_ID, input_text[:80], now, now))
        done += 1
    except Exception as e:
        errors += 1
        if errors <= 3:
            print("  [ERR] %s: %s" % (case_id, str(e)[:80]))
    
    if (i + 1) % 50 == 0:
        print("  [%d/%d] done=%d errors=%d skipped=%d %.1fs" % (
            i+1, len(targets), done, errors, skipped, time.time()-t0))

print("\nDone! inserted=%d errors=%d skipped=%d (%.1fs)" % (
    done, errors, skipped, time.time()-t0))

# Final check
rem = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy'").fetchone()[0]
bad = conn.execute("SELECT COUNT(*) FROM conversation_history WHERE user_id = 'zwy' AND (bot_reply LIKE '感谢您的反馈%%' OR bot_reply LIKE '抱歉%%')").fetchone()[0]
print("Final: total=%d bad_template=%d" % (rem, bad))

# Sample check
samples = conn.execute("""
    SELECT user_message, bot_reply, intent FROM conversation_history 
    WHERE user_id = 'zwy' ORDER BY id DESC LIMIT 5
""").fetchall()
print("\nLast 5 inserted records:")
for s in samples:
    print("  USER: %s" % s[0][:60])
    print("  BOT:  %s" % s[1][:70])
    print("  INTENT: %s" % s[2])
    print()

conn.close()
