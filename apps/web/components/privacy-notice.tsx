"use client";

import { Check, ExternalLink, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import { PRIVACY_NOTICE_VERSION } from "@/lib/privacy-consent";

type Props = {
  required: boolean;
  onAccept: () => void;
  onClose?: () => void;
};

export function PrivacyNotice({ required, onAccept, onClose }: Props) {
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => setConfirmed(false), [required]);

  return (
    <div className="privacy-notice-backdrop">
      <section
        className="privacy-notice-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="privacy-notice-title"
        aria-describedby="privacy-notice-summary"
      >
        <header>
          <span aria-hidden="true"><ShieldCheck size={22} /></span>
          <div>
            <p>PRIVACY &amp; SOURCE NOTICE / {PRIVACY_NOTICE_VERSION}</p>
            <h1 id="privacy-notice-title">使用前，请先了解这些边界</h1>
            <p id="privacy-notice-summary">
              这是一项学生个人开发的公益试用服务。以下内容说明数据如何处理、回答从哪里来，以及服务不代表谁。
            </p>
          </div>
        </header>

        <div className="privacy-notice-scroll" tabIndex={0}>
          <section>
            <i>01</i>
            <div>
              <h2>项目身份与立场</h2>
              <p>
                本项目由学生个人开发者 <b>Deliaru</b> 独立开发和维护，并非浙大城市学院官方产品。
                项目中的表述、回答和技术实现仅代表开发者个人，不代表浙大城市学院及其任何部门、教师或组织的官方立场；使用校名仅用于说明服务对象，不构成官方授权、认证或背书。
              </p>
              <p>
                服务当前以公益、非营利方式提供。所有信息仅供学习与办事参考，不应替代学校官网、主管部门通知或工作人员的正式答复。
              </p>
            </div>
          </section>

          <section>
            <i>02</i>
            <div>
              <h2>处理的数据与用途</h2>
              <ul>
                <li><b>必要的设备与安全数据：</b>匿名设备标识、会话与 CSRF 安全标识、请求时间、任务状态及必要的错误记录，用于维持会话、防止越权和排查故障。</li>
                <li><b>你主动提交的内容：</b>问题、对话、反馈，以及你选择填写或确认的培养层次、入学年份、学院、专业、目标、兴趣和待办，用于回答、历史恢复和个性化建议。</li>
                <li><b>本机偏好：</b>本声明的同意版本与时间、所选界面主题仅保存在当前浏览器。</li>
                <li><b>当前不主动读取：</b>个人成绩、课表、学分、选课状态、申请状态，也不要求你提供密码、Cookie、Ticket、Token 或其他账号凭据。</li>
              </ul>
              <p className="privacy-notice-warning">
                请勿在问题中输入身份证号、银行卡、健康信息、精确住址、账号密码等敏感个人信息，也不要提交他人的个人信息。
              </p>
            </div>
          </section>

          <section>
            <i>03</i>
            <div>
              <h2>大模型处理与信息来源</h2>
              <p>
                为生成回答，系统会把本次问题、必要的相关对话、你已确认启用的画像信息和检索到的材料，发送给当前由管理员配置的大模型接口处理。模型可能产生误解、遗漏或错误，不会自动替你办理任何校园业务。
              </p>
              <p>
                回答主要依据学校及其部门公开网页、公开文件和经批准的本地镜像，并尽量附带来源链接。网页更新、镜像同步延迟、材料适用范围变化或模型归纳错误都可能造成偏差；涉及时间、资格、费用、学籍和重要决定时，请以最新官方通知和主管部门答复为准。
              </p>
            </div>
          </section>

          <section>
            <i>04</i>
            <div>
              <h2>保存、安全与共享边界</h2>
              <ul>
                <li>匿名设备会话有效期最长为 180 日；会话、画像、待办和反馈为持续提供相应功能而保存，直至你主动删除、相关处理目的终止或服务停止运营。</li>
                <li>不同匿名设备主体的数据相互隔离；登录后合并匿名数据前会再次征求你的明确确认。</li>
                <li>服务不会出售个人信息，也不将个人信息用于广告画像。除提供大模型处理、保障服务安全或法律法规另有要求外，不主动向无关第三方提供。</li>
                <li>系统采用会话隔离、令牌哈希、CSRF 校验和访问控制等措施，但任何网络服务均无法承诺绝对安全。</li>
              </ul>
            </div>
          </section>

          <section>
            <i>05</i>
            <div>
              <h2>你的权利与联系方式</h2>
              <p>
                你可以在“我的空间”查看、更正或删除画像和待办，并在“数据”页删除当前主体的会话、画像、待办和反馈。你也可以撤回本声明的同意；撤回不影响撤回前基于同意已经进行的处理，但撤回后将无法继续使用服务。撤回同意本身不会自动删除既有数据，如需删除请先执行“删除全部个人数据”。
              </p>
              <p>
                对数据处理规则、权利请求或来源纠错有疑问，可通过
                <a href="https://github.com/Deliaru/HZCU_Agent/issues" target="_blank" rel="noreferrer">
                  项目 GitHub Issues <ExternalLink size={12} />
                </a>
                联系开发者。未满 14 周岁的用户请勿自行使用；确有需要时应由监护人阅读并同意。
              </p>
            </div>
          </section>

          <section>
            <i>06</i>
            <div>
              <h2>公益服务与禁止滥用</h2>
              <p>
                请合理使用服务。禁止批量自动请求、扫描攻击、绕过访问控制、恶意消耗模型或服务器资源、上传违法有害内容、冒充官方发布信息，以及以其他方式干扰服务或侵害他人权益。发现滥用时，开发者可以限制或停止相关访问。
              </p>
            </div>
          </section>

          <aside>
            <b>处理规则变更</b>
            <span>如处理目的、数据种类、共享对象或保存方式发生实质变化，本声明将更新版本并再次要求确认。</span>
          </aside>
        </div>

        <footer>
          {required ? (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                <span>我已完整阅读并同意《隐私数据处理与来源声明》，知悉本项目并非学校官方服务。</span>
              </label>
              <button type="button" disabled={!confirmed} onClick={onAccept}>
                <Check size={17} /> 接受并继续
              </button>
            </>
          ) : (
            <button type="button" onClick={onClose}>关闭声明</button>
          )}
        </footer>
      </section>
    </div>
  );
}
