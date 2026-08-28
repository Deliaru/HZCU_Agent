"use client";

import Image from "next/image";

export type CongyuScene =
  | "avatar"
  | "welcome"
  | "mobile-welcome"
  | "onboarding"
  | "library"
  | "hello"
  | "idle"
  | "working"
  | "success"
  | "error";

const SCENES: Record<
  CongyuScene,
  { src: string; width: number; height: number; alt: string; priority?: boolean }
> = {
  avatar: {
    src: "/themes/hzcu-girl/avatar.webp",
    width: 256,
    height: 256,
    alt: "琮羽微笑头像",
  },
  welcome: {
    src: "/themes/hzcu-girl/hero-desktop.webp",
    width: 941,
    height: 1672,
    alt: "琮羽向你伸出手，邀请你一起调查校园问题",
    priority: true,
  },
  "mobile-welcome": {
    src: "/themes/hzcu-girl/hero-mobile.webp",
    width: 941,
    height: 1672,
    alt: "琮羽向你打招呼",
    priority: true,
  },
  onboarding: {
    src: "/themes/hzcu-girl/guide-half.webp",
    width: 760,
    height: 1011,
    alt: "琮羽展示校园身份卡",
  },
  library: {
    src: "/themes/hzcu-girl/guide-half.webp",
    width: 760,
    height: 1011,
    alt: "琮羽在校园资料馆整理资料",
  },
  hello: {
    src: "/themes/hzcu-girl/chibi-hello.webp",
    width: 560,
    height: 512,
    alt: "琮羽挥手欢迎你",
  },
  idle: {
    src: "/themes/hzcu-girl/chibi-idle.webp",
    width: 512,
    height: 512,
    alt: "琮羽微笑等待",
  },
  working: {
    src: "/themes/hzcu-girl/chibi-work.webp",
    width: 512,
    height: 512,
    alt: "琮羽正在思考和查阅资料",
  },
  success: {
    src: "/themes/hzcu-girl/chibi-success.webp",
    width: 512,
    height: 512,
    alt: "琮羽完成了调查",
  },
  error: {
    src: "/themes/hzcu-girl/chibi-error.webp",
    width: 512,
    height: 512,
    alt: "琮羽对当前信息有些疑惑",
  },
};

export function CongyuArtwork({
  scene,
  className = "",
  sizes = "(max-width: 840px) 80vw, 42vw",
}: {
  scene: CongyuScene;
  className?: string;
  sizes?: string;
}) {
  const asset = SCENES[scene];
  return (
    <span className={`congyu-artwork congyu-artwork-${scene} ${className}`.trim()}>
      <Image
        src={asset.src}
        width={asset.width}
        height={asset.height}
        alt={asset.alt}
        priority={asset.priority}
      sizes={sizes}
      unoptimized
    />
    </span>
  );
}
