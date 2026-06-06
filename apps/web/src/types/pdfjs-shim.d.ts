// pdfjs-dist v5 的类型声明引用了 `ImageDataArray`，这是 TS 5.9+ DOM lib 才内置的
// 类型；本项目 TS 5.7 的 DOM lib 还没有它，导致 `tsc` 在 node_modules 里报
// 「Cannot find name 'ImageDataArray'」。这里补一个等价的全局别名兜底。
// 升级到 TS 5.9+ 后可删除本文件。
declare global {
  type ImageDataArray = Uint8ClampedArray;
}

export {};
