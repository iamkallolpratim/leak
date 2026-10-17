import { Config } from "@remotion/cli/config";

// Screenshots live in docs/assets (committed, referenced by the README) and are
// served to the video through staticFile("name.png"): one copy, no duplicates.
Config.setPublicDir("../docs/assets");
Config.setOverwriteOutput(true);
Config.setVideoImageFormat("jpeg");
Config.setJpegQuality(92);
