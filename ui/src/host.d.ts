declare module '@kirocrew/app-sdk' {
  export type AppApi = {
    raw(path: string, init?: RequestInit): Promise<Response>
    request<T>(path: string, init?: RequestInit): Promise<T>
    get<T>(path: string): Promise<T>
    post<T>(path: string, body?: unknown): Promise<T>
    del<T>(path: string, init?: RequestInit): Promise<T>
  }

  export function useAppApi(): AppApi
  export function fmtDateTime(value: string | number | Date): string
}

declare module '@kirocrew/app-sdk/ui' {
  import type { ComponentType, InputHTMLAttributes, ReactNode } from 'react'

  type CommonProps = { children?: ReactNode; className?: string }
  export const Btn: ComponentType<
    CommonProps & { onClick?: () => void; disabled?: boolean }
  >
  export const Card: ComponentType<CommonProps>
  export const CardTitle: ComponentType<CommonProps>
  export const EmptyState: ComponentType<{
    icon: ReactNode
    title: string
    subtitle?: string
  }>
  export const ErrorNotice: ComponentType<{
    title?: string
    message?: string | null
    askAgent?: boolean
  }>
  export const Input: ComponentType<InputHTMLAttributes<HTMLInputElement>>
  export const MarkdownRenderer: ComponentType<{ content: string }>
  export const PageHeader: ComponentType<{ title: string; subtitle?: string }>
  export const StatCard: ComponentType<{
    label: string
    value: string
    accent?: boolean
  }>
}
