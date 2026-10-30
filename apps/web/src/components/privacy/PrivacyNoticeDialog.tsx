import { Dialog } from '../../admin/components/Dialog';
import type { PrivacyNotice } from '../../api/privacy';
import {
  countryLabel,
  legalBasisLabel,
  mentionsComplaint,
  pluralize,
  rightLabel,
  transferMechanismLabel,
} from './labels';
import { AppLink, btn, InlineAlert, linkCls } from './ui';

/** Profile → "Privacy & data" (ProfilePage reads `?tab=`). */
export const PRIVACY_TAB_PATH = '/profile?tab=privacy';

export function PrivacyTabLink({ children }: { children: React.ReactNode }) {
  return <AppLink to={PRIVACY_TAB_PATH}>{children}</AppLink>;
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="text-[12.5px] font-semibold text-ink tracking-[-0.005em]">{title}</h3>
      {children}
    </section>
  );
}

function Bullets({ children }: { children: React.ReactNode }) {
  return <ul className="list-disc pl-5 flex flex-col gap-1">{children}</ul>;
}

function NoticeBody({ notice, privacyLink }: { notice: PrivacyNotice; privacyLink: React.ReactNode }) {
  const controller = notice.controller ?? { name: '', contact_email: '' };
  const purposes = notice.purposes ?? [];
  const processors = notice.processors ?? [];
  const rights = notice.rights ?? [];
  const retentionDays = notice.retention?.session_retention_days ?? null;
  const trashDays = notice.retention?.trash_grace_days;
  const contact = controller.contact_email ? (
    <a href={`mailto:${controller.contact_email}`} className={linkCls}>{controller.contact_email}</a>
  ) : null;

  return (
    <>
      <Block title="Who's responsible for your data">
        <p>
          {controller.name ? <span className="text-ink">{controller.name}</span> : 'Your organisation'} is the
          data controller{contact ? <> — questions go to {contact}</> : null}.
        </p>
        {notice.dpo_email && (
          <p>
            Data protection officer:{' '}
            <a href={`mailto:${notice.dpo_email}`} className={linkCls}>{notice.dpo_email}</a>
          </p>
        )}
      </Block>

      <Block title="What we use it for">
        {purposes.length === 0 ? (
          <p>Running the tasks you ask for and keeping the service secure.</p>
        ) : (
          <Bullets>
            {purposes.map((p, i) => (
              <li key={`${p.purpose}-${i}`}>
                <span className="text-ink">{p.purpose}</span>
                <span className="text-dim"> — legal basis: {legalBasisLabel(p.legal_basis)}</span>
              </li>
            ))}
          </Bullets>
        )}
      </Block>

      <Block title="How long we keep it">
        <Bullets>
          <li>
            {retentionDays != null && retentionDays > 0
              ? `Tasks and conversations are kept for ${pluralize(retentionDays, 'day')}, then deleted automatically.`
              : 'Tasks and conversations are kept until you delete them.'}
          </li>
          {typeof trashDays === 'number' && (
            <li>
              {trashDays > 0
                ? `Anything you delete stays in the trash for ${pluralize(trashDays, 'day')}, then it's removed for good.`
                : "Anything you delete is removed for good straight away."}
            </li>
          )}
        </Bullets>
      </Block>

      <Block title="Memory">
        {notice.memory?.enabled ? (
          <p>
            Memory is on: when the agent finishes a reply or a task, a word-for-word excerpt of it (the start of
            your request and of the agent's answer) is saved and can be reused as context in your later
            conversations and tasks. You can turn this off or delete saved memories {privacyLink}.
          </p>
        ) : (
          <p>Memory is off, so nothing from your conversations or tasks is saved for reuse later.</p>
        )}
      </Block>

      <Block title="Who else handles it">
        {processors.length === 0 ? (
          <p>No outside service providers are listed.</p>
        ) : (
          <>
            <p>
              These service providers process data for us. In brackets: where they are, and the safeguard for
              transfers outside the EU/EEA.
            </p>
            <Bullets>
              {processors.map((p, i) => {
                const where = [countryLabel(p.country), transferMechanismLabel(p.transfer_mechanism)]
                  .filter(Boolean)
                  .join(' · ');
                return (
                  <li key={`${p.name}-${i}`}>
                    <span className="text-ink">{p.name}</span>
                    {p.role ? ` — ${p.role}` : ''}
                    {where && <span className="text-dim"> ({where})</span>}
                  </li>
                );
              })}
            </Bullets>
          </>
        )}
      </Block>

      <Block title="Your rights">
        <Bullets>
          {rights.map((r, i) => (
            <li key={`${r}-${i}`}>{rightLabel(r)}</li>
          ))}
          {!mentionsComplaint(rights) && (
            <li>Complain to a data protection supervisory authority, for example where you live or work</li>
          )}
        </Bullets>
        <p>
          You can download or delete your data {privacyLink}
          {contact ? <>; for anything else, contact {contact}</> : null}.
        </p>
      </Block>
    </>
  );
}

// Shown when GET /privacy/notice fails. Deliberately general: no retention
// figures or names we can't vouch for.
function FallbackBody({ privacyLink }: { privacyLink: React.ReactNode }) {
  return (
    <>
      <InlineAlert tone="warn">We couldn't load the full privacy notice just now, so here's the short version.</InlineAlert>
      <Block title="What we collect">
        <p>The tasks you run, your messages, the files you share, and basic account and usage data.</p>
      </Block>
      <Block title="Why">
        <p>
          To run the tasks you ask for, to keep the service secure — administrators may review conversations for
          security audits — and to meet legal obligations.
        </p>
      </Block>
      <Block title="How long">
        <p>Only as long as it's needed. Anything you delete is removed for good after a short grace period.</p>
      </Block>
      <Block title="Your rights">
        <p>
          You can see, correct, download or delete your data, restrict or object to how it's used, and complain
          to a data protection supervisory authority. Download or delete your data {privacyLink}; ask your
          administrator for the full notice, including who is responsible for your data.
        </p>
      </Block>
    </>
  );
}

export interface PrivacyNoticeDialogProps {
  /** null renders the built-in summary (notice endpoint unavailable). */
  notice: PrivacyNotice | null;
  /** ESC / backdrop / Close. */
  onClose: () => void;
  /** When set, the primary action is "Got it" and records the acknowledgement. */
  onAcknowledge?: () => void;
  busy?: boolean;
  error?: string | null;
  /** Link to Profile → Privacy & data (off when already on that page). */
  showPrivacyLink?: boolean;
}

export function PrivacyNoticeDialog({
  notice,
  onClose,
  onAcknowledge,
  busy = false,
  error = null,
  showPrivacyLink = true,
}: PrivacyNoticeDialogProps) {
  const privacyLink = showPrivacyLink ? (
    <>in <PrivacyTabLink>Profile → Privacy &amp; data</PrivacyTabLink></>
  ) : (
    'on this page'
  );

  return (
    <Dialog title="Privacy notice" onClose={onClose} dismissable={!busy} className="w-full max-w-[560px]">
      <div className="flex flex-col gap-4 max-h-[min(62vh,560px)] overflow-y-auto pr-1 text-[13px] leading-[1.6] text-ink-2">
        <p className="text-ink">
          Here's how your data is handled when you use this workspace.
          {showPrivacyLink ? ' You can reread this any time from Profile → Privacy & data.' : ''}
        </p>
        {notice ? <NoticeBody notice={notice} privacyLink={privacyLink} /> : <FallbackBody privacyLink={privacyLink} />}
      </div>

      {error && <InlineAlert tone="error">{error}</InlineAlert>}

      <div className="flex items-center gap-2 pt-1">
        <span className="font-mono text-[10.5px] text-dim">
          {notice ? `Version ${notice.version}` : 'Summary'}
        </span>
        <div className="flex-1" />
        {onAcknowledge ? (
          <>
            {error && (
              <button type="button" className={btn.ghost} onClick={onClose} disabled={busy}>
                Close
              </button>
            )}
            <button type="button" className={btn.danger} onClick={onAcknowledge} disabled={busy}>
              {busy ? 'Saving…' : error ? 'Try again' : 'Got it'}
            </button>
          </>
        ) : (
          <button type="button" className={btn.base} onClick={onClose}>
            Close
          </button>
        )}
      </div>
    </Dialog>
  );
}
