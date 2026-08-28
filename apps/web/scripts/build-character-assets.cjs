const sharp = require("sharp");

const source = process.argv[2];
const output = process.argv[3];

async function webp(input, name, resize, quality = 82) {
  await sharp(input)
    .resize(resize)
    .webp({ quality, alphaQuality: 95, effort: 5 })
    .toFile(`${output}/${name}`);
}

async function main() {
  await Promise.all([
    webp(`${source}/立绘3.png`, "hero-desktop.webp", { width: 941 }, 84),
    // Keep the approved mobile artwork at its native width. A 760px derivative
    // is visibly soft on 3x mobile displays even when its CSS width is 320px.
    webp(`${source}/立绘4.png`, "hero-mobile.webp", { width: 941 }, 80),
    webp(
      `${source}/立绘1-2.png`,
      "guide-half.webp",
      {
        width: 760,
        height: 1011,
        fit: "contain",
        position: "north",
        background: { r: 0, g: 0, b: 0, alpha: 0 },
      },
      83,
    ),
    webp(`${source}/SD/微笑.png`, "avatar.webp", { width: 256, height: 256, fit: "cover" }),
    webp(`${source}/SD/微笑.png`, "chibi-idle.webp", { width: 512, height: 512, fit: "contain" }, 80),
    webp(`${source}/SD/打招呼.png`, "chibi-hello.webp", { width: 560, height: 512, fit: "contain" }, 80),
    webp(`${source}/SD/思考.png`, "chibi-work.webp", { width: 512, height: 512, fit: "contain" }, 80),
    webp(`${source}/SD/得意.png`, "chibi-success.webp", { width: 512, height: 512, fit: "contain" }, 80),
    webp(`${source}/SD/疑惑.png`, "chibi-error.webp", { width: 512, height: 512, fit: "contain" }, 80),
    webp(`${source}/抽象鹰翼.png`, "wing-field.webp", { width: 1200, height: 1200, fit: "contain" }, 80),
    webp(`${source}/羽毛.png`, "feather-mark.webp", { width: 512, height: 512, fit: "contain" }, 80),
  ]);

  const hero = await sharp(`${source}/立绘4.png`)
    .resize({ height: 560 })
    .webp({ quality: 82, alphaQuality: 95 })
    .toBuffer();
  const wing = await sharp(`${source}/抽象鹰翼.png`)
    .resize({ width: 480 })
    .modulate({ brightness: 1.3, saturation: 0.7 })
    .webp({ quality: 70, alphaQuality: 80 })
    .toBuffer();
  const characterUi = Buffer.from(`
    <svg width="960" height="600" xmlns="http://www.w3.org/2000/svg">
      <rect x="48" y="50" width="500" height="500" rx="28" fill="#fff" fill-opacity=".92"/>
      <rect x="82" y="92" width="210" height="18" rx="9" fill="#397cc2"/>
      <rect x="82" y="134" width="368" height="48" rx="8" fill="#183a63"/>
      <rect x="82" y="204" width="400" height="12" rx="6" fill="#8ba7c2"/>
      <rect x="82" y="230" width="350" height="12" rx="6" fill="#b8cadb"/>
      <rect x="82" y="305" width="120" height="38" rx="10" fill="#397cc2"/>
      <rect x="214" y="305" width="120" height="38" rx="10" fill="#eaf2fa"/>
    </svg>`,
  );
  await sharp({ create: { width: 960, height: 600, channels: 4, background: "#eaf3fc" } })
    .composite([
      { input: wing, left: 58, top: 60, blend: "over" },
      { input: hero, left: 560, top: 36 },
      { input: characterUi },
    ])
    .webp({ quality: 82, effort: 5 })
    .toFile(`${output}/theme-character-preview.webp`);

  const minimalUi = Buffer.from(`
    <svg width="960" height="600" xmlns="http://www.w3.org/2000/svg">
      <rect x="40" y="35" width="880" height="530" rx="18" fill="#fff"/>
      <rect x="40" y="35" width="64" height="530" fill="#142b4b"/>
      <rect x="128" y="64" width="300" height="16" rx="8" fill="#356fae"/>
      <rect x="128" y="104" width="520" height="76" rx="8" fill="#142b4b"/>
      <rect x="128" y="214" width="600" height="12" rx="6" fill="#7890aa"/>
      <rect x="128" y="240" width="560" height="12" rx="6" fill="#b4c0ce"/>
      <rect x="128" y="300" width="220" height="70" rx="8" fill="#edf2f7"/>
      <rect x="366" y="300" width="220" height="70" rx="8" fill="#edf2f7"/>
      <rect x="604" y="300" width="220" height="70" rx="8" fill="#edf2f7"/>
      <rect x="128" y="452" width="696" height="66" rx="16" fill="#f8fafc" stroke="#dce3eb" stroke-width="2"/>
      <circle cx="788" cy="485" r="22" fill="#356fae"/>
    </svg>`,
  );
  await sharp({ create: { width: 960, height: 600, channels: 4, background: "#edf2f7" } })
    .composite([{ input: minimalUi }])
    .webp({ quality: 82, effort: 5 })
    .toFile(`${output}/theme-minimal-preview.webp`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
