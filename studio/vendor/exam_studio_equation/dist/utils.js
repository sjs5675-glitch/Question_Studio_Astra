"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isAlpha = isAlpha;
exports.isDigit = isDigit;
function isAlpha(ch) { return /^[A-Za-z]$/.test(ch); }
function isDigit(ch) { return /^[0-9]$/.test(ch); }
