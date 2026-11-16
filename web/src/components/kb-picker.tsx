import { useEffect, useRef, useState, type FormEvent } from "react"

import type { KbSummary } from "@/lib/library"
import { createKb, listKbs } from "@/lib/library"
import { Logo } from "@/components/logo"
import { SplitTextReveal } from "@/components/motion/split-text-reveal"
import { Button } from "@/components/ui/button"
import { IconFolder } from "@/components/spectrumui/blocks/empty-states/empty-state-kit"
import { ProjectEmpty } from "@/components/spectrumui/blocks/empty-states/project-empty"
import { toast } from "@/components/toaster"

function ago(t: number) {
  const m = Math.round((Date.now() - t * 1000) / 60000)
  if (m < 1) return "刚刚"
  if (m < 60) return `${m} 分钟前`
  if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`
  return `${Math.round(m / 60 / 24)} 天前`
}

const fail = (title: string, e: unknown) =>
  toast({ title, description: e instanceof Error ? e.message : String(e), status: "error" })

/** 首页：服务器上的全部知识库，选择一个进入，或新建。 */
export function KbPicker({ onOpen }: { onOpen: (name: string) => Promise<void> }) {
  const [kbs, setKbs] = useState<KbSummary[] | null>(null)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [chineseName, setChineseName] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState("")
  const busy = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    listKbs().then(
      (r) => {
        setKbs(r.kbs)
        setStorageError(r.storage.writable ? null : `服务器文件库目录不可写：${r.storage.path}`)
      },
      (e) => {
        setKbs([])
        fail("无法读取知识库列表", e)
      },
    )
  }, [])

  useEffect(() => {
    if (creating) inputRef.current?.focus()
  }, [creating])

  const run = async (task: () => Promise<void>) => {
    if (busy.current) return
    busy.current = true
    try {
      await task()
    } finally {
      busy.current = false
    }
  }

  const open = (kb: string) => run(() => onOpen(kb).catch((e) => fail("无法打开知识库", e)))

  const create = (e: FormEvent) => {
    e.preventDefault()
    const value = name.trim()
    if (!value) return
    void run(async () => {
      try {
        const { name: created } = await createKb(value)
        await onOpen(created)
      } catch (err) {
        fail("新建知识库失败", err)
      }
    })
  }

  const list = kbs ?? []
  const showForm = creating || (kbs !== null && list.length === 0)

  return (
    <div className="flex min-h-svh flex-col items-center bg-muted/30 px-4 pt-14 pb-10">
      <h1>
        {/* 点击品牌名在中英文之间切换，每次切换重播逐字飞入 */}
        <button
          className="group flex cursor-pointer select-none flex-col items-center gap-4"
          onClick={() => setChineseName((v) => !v)}
          type="button"
        >
          <Logo className="size-20" />
          <SplitTextReveal
            className="font-bold text-[40px] text-neutral-900 leading-none tracking-tight transition-transform duration-200 group-hover:scale-[1.02] dark:text-neutral-100"
            text={chineseName ? "层析" : "Stratum"}
          />
        </button>
      </h1>
      <div className="flex w-full flex-1 flex-col items-center justify-center gap-4 py-8">
        {storageError && (
          <p className="max-w-[680px] rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-900/40 dark:bg-red-900/20 dark:text-red-300">
            {storageError}，请联系管理员检查 STRATUM_DATA_DIR 配置。
          </p>
        )}
        <ProjectEmpty
          // 列表变化后重置组件内的选中状态
          key={list.map((kb) => kb.name).join("/")}
          chosenLabel={(t) => `打开「${t.name}」`}
          className="max-w-[680px]"
          createLabel="新建知识库"
          description="每个知识库对应 FastGPT 中的一个知识库，PDF 和解析结果都保存在服务器上。选择你负责的知识库进入。"
          footnote={list.length ? "双击知识库可直接打开。" : undefined}
          meta={kbs === null ? "加载中" : `共 ${list.length} 个知识库`}
          onActivate={(id) => void open(id)}
          onCreate={(id) => (id ? void open(id) : setCreating(true))}
          panelTitle="知识库"
          secondaryAction={() => null}
          templates={list.map((kb) => ({
            id: kb.name,
            name: kb.name,
            detail: `${kb.pdfs} 个 PDF · 已转换 ${kb.converted} · ${ago(kb.updated)}`,
            icon: <IconFolder />,
          }))}
          title={list.length ? "选择一个知识库" : "还没有知识库"}
          toolbarLabel="新建知识库"
          variant={list.length ? "Templates" : "Minimal"}
        />
        {showForm && (
          <form
            className="flex w-full max-w-[680px] flex-wrap items-center gap-2 rounded-2xl border border-border bg-card px-4 py-3 shadow-xs"
            onSubmit={create}
          >
            <label className="text-sm font-medium" htmlFor="kb-name">新建知识库</label>
            <input
              className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              id="kb-name"
              maxLength={64}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：安全规程库（建议与 FastGPT 中的知识库同名）"
              ref={inputRef}
              value={name}
            />
            <Button disabled={!name.trim()} type="submit">创建并打开</Button>
            {list.length > 0 && (
              <Button onClick={() => setCreating(false)} type="button" variant="ghost">取消</Button>
            )}
          </form>
        )}
      </div>
    </div>
  )
}
