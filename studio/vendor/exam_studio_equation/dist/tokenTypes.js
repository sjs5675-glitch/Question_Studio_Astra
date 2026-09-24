"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TokenType = void 0;
var TokenType;
(function (TokenType) {
    TokenType[TokenType["KEYWORD"] = 0] = "KEYWORD";
    TokenType[TokenType["IDENT"] = 1] = "IDENT";
    TokenType[TokenType["NUMBER"] = 2] = "NUMBER";
    TokenType[TokenType["SYMBOL"] = 3] = "SYMBOL";
    TokenType[TokenType["SPACE"] = 4] = "SPACE";
    TokenType[TokenType["EOF"] = 5] = "EOF";
    TokenType[TokenType["UNKNOWN"] = 6] = "UNKNOWN";
})(TokenType || (exports.TokenType = TokenType = {}));
