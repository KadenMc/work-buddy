import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  CheckCircle,
  Info,
  Warning,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import {
  Dialog,
  Heading,
  Modal,
  ModalOverlay,
} from "react-aria-components";

import { Button } from "../../ui";

export type TransientNoticeTone = "info" | "success" | "warning" | "danger";

export interface TransientNoticeRequest {
  readonly message: string;
  readonly tone?: TransientNoticeTone;
  readonly ttlMs?: number;
  readonly dedupeKey?: string;
  readonly action?: {
    readonly label: string;
    run(): void;
  };
}

interface TransientNotice extends TransientNoticeRequest {
  readonly id: number;
  readonly tone: TransientNoticeTone;
  readonly ttlMs: number;
}

export interface ConfirmationRequest {
  readonly title: string;
  readonly description: string;
  readonly confirmLabel: string;
  readonly cancelLabel?: string;
  readonly tone?: "default" | "danger";
}

interface PendingConfirmation extends ConfirmationRequest {
  readonly id: number;
  resolve(confirmed: boolean): void;
}

interface InteractionSurfaceRuntime {
  notify(request: TransientNoticeRequest): number;
  dismissNotice(id: number): void;
  confirm(request: ConfirmationRequest): Promise<boolean>;
}

const InteractionSurfaceContext = createContext<InteractionSurfaceRuntime | null>(null);

const noticeIcon = (tone: TransientNoticeTone) => {
  if (tone === "success") return <CheckCircle weight="fill" aria-hidden="true" />;
  if (tone === "warning") return <Warning weight="fill" aria-hidden="true" />;
  if (tone === "danger") return <WarningCircle weight="fill" aria-hidden="true" />;
  return <Info weight="fill" aria-hidden="true" />;
};

function TransientNoticeCard({
  notice,
  onDismiss,
}: {
  readonly notice: TransientNotice;
  onDismiss(): void;
}) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const timeout = window.setTimeout(onDismiss, notice.ttlMs);
    return () => window.clearTimeout(timeout);
  }, [notice.ttlMs, onDismiss, paused]);
  return (
    <div
      className={`wb-transient-notice wb-transient-notice--${notice.tone}`}
      role={notice.tone === "danger" ? "alert" : "status"}
      aria-atomic="true"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false);
      }}
    >
      <span className="wb-transient-notice__icon">{noticeIcon(notice.tone)}</span>
      <span className="wb-transient-notice__message">{notice.message}</span>
      {notice.action ? (
        <Button
          size="small"
          variant="ghost"
          onClick={() => {
            notice.action?.run();
            onDismiss();
          }}
        >
          {notice.action.label}
        </Button>
      ) : null}
      <Button
        className="wb-transient-notice__dismiss"
        size="small"
        variant="ghost"
        aria-label="Dismiss notification"
        onClick={onDismiss}
      >
        <X aria-hidden="true" />
      </Button>
    </div>
  );
}

export function InteractionSurfaceProvider({ children }: { readonly children: ReactNode }) {
  const nextId = useRef(0);
  const [notices, setNotices] = useState<readonly TransientNotice[]>([]);
  const [confirmations, setConfirmations] = useState<readonly PendingConfirmation[]>([]);
  const confirmationsRef = useRef(confirmations);
  confirmationsRef.current = confirmations;

  const dismissNotice = useCallback((id: number) => {
    setNotices((current) => current.filter((notice) => notice.id !== id));
  }, []);
  const notify = useCallback((request: TransientNoticeRequest): number => {
    const id = ++nextId.current;
    const notice: TransientNotice = {
      ...request,
      id,
      tone: request.tone ?? "info",
      ttlMs: Math.max(5_000, request.ttlMs ?? 7_000),
    };
    setNotices((current) => {
      const withoutDuplicate =
        request.dedupeKey === undefined
          ? current
          : current.filter((item) => item.dedupeKey !== request.dedupeKey);
      return [...withoutDuplicate, notice].slice(-3);
    });
    return id;
  }, []);
  const confirm = useCallback(
    (request: ConfirmationRequest): Promise<boolean> =>
      new Promise((resolve) => {
        setConfirmations((current) => [
          ...current,
          { ...request, id: ++nextId.current, resolve },
        ]);
      }),
    [],
  );
  const settleConfirmation = useCallback((confirmed: boolean) => {
    const active = confirmationsRef.current[0];
    if (active === undefined) return;
    active.resolve(confirmed);
    setConfirmations((current) => current.filter((item) => item.id !== active.id));
  }, []);
  useEffect(
    () => () => {
      confirmationsRef.current.forEach((confirmation) => confirmation.resolve(false));
    },
    [],
  );
  const runtime = useMemo(
    () => ({ notify, dismissNotice, confirm }),
    [confirm, dismissNotice, notify],
  );
  const activeConfirmation = confirmations[0];

  return (
    <InteractionSurfaceContext.Provider value={runtime}>
      {children}
      <div className="wb-transient-notice-region">
        {notices.map((notice) => (
          <TransientNoticeCard
            key={notice.id}
            notice={notice}
            onDismiss={() => dismissNotice(notice.id)}
          />
        ))}
      </div>
      {activeConfirmation ? (
        <ModalOverlay
          className="wb-confirmation-overlay"
          isOpen
          isDismissable
          onOpenChange={(open) => {
            if (!open) settleConfirmation(false);
          }}
        >
          <Modal className="wb-confirmation-modal">
            <Dialog
              className="wb-confirmation-dialog"
              role={activeConfirmation.tone === "danger" ? "alertdialog" : "dialog"}
            >
              <Heading slot="title" className="wb-confirmation-dialog__title">
                {activeConfirmation.title}
              </Heading>
              <p className="wb-confirmation-dialog__description">
                {activeConfirmation.description}
              </p>
              <div className="wb-confirmation-dialog__actions">
                <Button onClick={() => settleConfirmation(false)}>
                  {activeConfirmation.cancelLabel ?? "Cancel"}
                </Button>
                <Button
                  variant={activeConfirmation.tone === "danger" ? "danger" : "primary"}
                  onClick={() => settleConfirmation(true)}
                >
                  {activeConfirmation.confirmLabel}
                </Button>
              </div>
            </Dialog>
          </Modal>
        </ModalOverlay>
      ) : null}
    </InteractionSurfaceContext.Provider>
  );
}

export function useInteractionSurfaces(): InteractionSurfaceRuntime {
  const runtime = useContext(InteractionSurfaceContext);
  if (runtime === null) {
    throw new Error("useInteractionSurfaces must run inside InteractionSurfaceProvider");
  }
  return runtime;
}
