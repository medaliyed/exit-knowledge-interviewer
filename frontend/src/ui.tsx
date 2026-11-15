import * as Dialog from '@radix-ui/react-dialog';
import { Slot } from '@radix-ui/react-slot';
import { cva } from 'class-variance-authority';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { forwardRef, useId, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import type { Citation } from './types';

export function cn(...values: ClassValue[]) { return twMerge(clsx(values)); }

// Locally owned shadcn-style components: Radix behavior, styled with eki's tokens.
const buttonVariants = cva('btn', {
  variants: { variant: { primary: 'btn-primary', quiet: 'btn-quiet', danger: 'btn-danger' } },
});
export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'quiet' | 'danger'; asChild?: boolean;
}>(function Button({ className, variant, asChild = false, type = 'button', ...props }, ref) {
  const Component = asChild ? Slot : 'button';
  return <Component type={type} className={cn(buttonVariants({ variant }), className)} ref={ref} {...props} />;
});

export function Badge({ status = '', children }: { status?: string; children: ReactNode }) {
  return <span className={`badge badge-${status}`}><span aria-hidden="true" className={`status-mark status-${status}`} />{children}</span>;
}
export function Notice({ error = false, children }: { error?: boolean; children: ReactNode }) {
  return <div className={`notice ${error ? 'notice-warn' : 'notice-ok'}`} role={error ? 'alert' : 'status'}>
    <span aria-hidden="true" className={`status-mark ${error ? 'status-error' : 'status-noted'}`} />
    <div>{children}</div>
  </div>;
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><span className="empty-symbol" aria-hidden="true">↗</span><h2>{title}</h2><div>{children}</div></div>;
}
export function Loading() { return <p className="loading" role="status"><span className="spinner" aria-hidden="true" />Opening your saved work…</p>; }

export function Modal({ title, open, onClose, children }: {
  title: string; open: boolean; onClose: () => void; children: ReactNode;
}) {
  const description = useId();
  const previousFocus = useRef<HTMLElement | null>(null);
  return <Dialog.Root open={open} onOpenChange={value => { if (!value) onClose(); }}>
    <Dialog.Portal><div className="dialog-overlay" aria-hidden="true" />
      <Dialog.Content className="dialog" aria-describedby={description} onOpenAutoFocus={() => {
        previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      }} onCloseAutoFocus={event => {
        event.preventDefault();
        const target = previousFocus.current?.isConnected ? previousFocus.current : document.getElementById('main-content');
        target?.focus({ preventScroll: true });
      }}>
        <div className="row between"><Dialog.Title>{title}</Dialog.Title>
          <Dialog.Close className="icon-button" aria-label="Close dialog">×</Dialog.Close></div>
        <Dialog.Description id={description} className="sr-only">{title}. Review the details below.</Dialog.Description>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}

export function Source({ citation, onClick }: { citation: Citation; onClick?: () => void }) {
  const label = citation.kind === 'artifact' ? citation.artifact_id : `${citation.author_name ? `Maintainer ${citation.author_name}` : 'interview'} · turn ${citation.turn_index}`;
  return onClick ? <button type="button" className="source" onClick={onClick}>{label}</button>
    : <span className="source source-reference">{label}</span>;
}
export function SectionLabel({ name }: { name: string }) { return <span className="section-label">{name}</span>; }
