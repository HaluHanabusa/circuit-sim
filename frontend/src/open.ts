// 仕様JSONを開く処理の、画面の側の確かめ（画面仕様 4.2 の 1〜4）。5〜8 はアプリ本体が確かめる。
// 上から順に確かめ、最初に引っかかった所で止めて、その理由（お知らせの番号と差し込む値）を返す。
import { api, type Doc } from "./api";
import { formatQuantity, SCHEMATIC_DIGITS } from "./num/format";
import type { NoticeData } from "./notices";

export const MAX_SPEC_BYTES = 10 * 1000 * 1000;

export type OpenResult = { ok: true; doc: Doc } | { ok: false; notice: NoticeData; file?: string };

function fail(notice: NoticeData, file?: string): OpenResult {
  return { ok: false, notice, file };
}

export async function readSpecFile(files: readonly File[]): Promise<OpenResult> {
  if (files.length !== 1) return fail({ code: "N-OPEN-MULTI", params: { n: files.length } });
  const file = files[0];
  const name = file.name;
  if (!name.toLowerCase().endsWith(".json")) return fail({ code: "N-OPEN-TYPE", params: { file: name } }, name);
  if (file.size > MAX_SPEC_BYTES) {
    const size = formatQuantity(file.size, "B", SCHEMATIC_DIGITS).text;
    return fail({ code: "N-OPEN-SIZE", params: { file: name, size } }, name);
  }
  let bytes: ArrayBuffer;
  try {
    bytes = await file.arrayBuffer();
  } catch (exc) {
    return fail({ code: "N-OPEN-READ", params: { file: name }, details: String(exc) }, name);
  }
  let text: string;
  try {
    // 先頭の BOM は外して読む（TextDecoder の既定）。UTF-8 として読めなければ止める
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return fail({ code: "N-OPEN-ENCODING", params: { file: name } }, name);
  }
  const response = await api.openText(name, text);
  return response.ok ? response : fail(response.notice, name);
}

export async function openSample(id: string): Promise<OpenResult> {
  const response = await api.openSample(id);
  return response.ok ? response : fail(response.notice);
}
