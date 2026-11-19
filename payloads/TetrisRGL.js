(async () => {
    try {
        const id = "classic-tetris-game";

        if (window._tetrisInterval) {
            clearInterval(window._tetrisInterval);
            window._tetrisInterval = null;
        }

        const old = document.getElementById(id);
        if (old) old.remove();

        const canvas = document.createElement("canvas");
        canvas.id = id;
        canvas.style.position = "fixed";
        canvas.style.top = "0";
        canvas.style.left = "0";
        canvas.style.width = "100vw";
        canvas.style.height = "100vh";
        canvas.style.zIndex = "2147483647";
        canvas.style.backgroundColor = "#060913";
        canvas.style.pointerEvents = "none";

        document.body.appendChild(canvas);
        const ctx = canvas.getContext("2d");

        const COLS = 10;
        const ROWS = 20;

        let BLOCK = 28;
        let BOARD_W = COLS * BLOCK;
        let BOARD_H = ROWS * BLOCK;
        let OFFSET_X = 0;
        let OFFSET_Y = 0;

        function resizeGame() {
            canvas.width = window.innerWidth || document.documentElement.clientWidth || 1280;
            canvas.height = window.innerHeight || document.documentElement.clientHeight || 720;

            const maxBoardH = canvas.height * 0.84;
            const maxBoardW = canvas.width * 0.42;

            BLOCK = Math.floor(Math.min(maxBoardH / ROWS, maxBoardW / COLS));
            BOARD_W = COLS * BLOCK;
            BOARD_H = ROWS * BLOCK;

            OFFSET_X = Math.floor((canvas.width - BOARD_W) / 2);
            OFFSET_Y = Math.floor((canvas.height - BOARD_H) / 2) + 15;
        }
        resizeGame();
        window.addEventListener("resize", resizeGame);

        const stars = Array.from({ length: 45 }, () => ({
            x: Math.random() * canvas.width,
            y: Math.random() * canvas.height,
            size: Math.random() * 1.8 + 0.6,
            alpha: Math.random() * 0.5 + 0.2,
            pulseSpeed: Math.random() * 0.02 + 0.01
        }));

        let board = Array.from({ length: ROWS }, () => Array(COLS).fill(0));
        let score = 0;
        let linesClearedTotal = 0;
        let level = 1;
        let gameOver = false;

        let particles = [];
        let lineFlashes = [];
        let shockwaves = [];
        let tetrisBanners = [];
        let shakeFrames = 0;
        let shakeIntensity = 0;

        const PIECES = [
            { shape: [[1, 1, 1, 1]], color: "#00e5ff" },          // I
            { shape: [[1, 0, 0], [1, 1, 1]], color: "#3b82f6" },  // J
            { shape: [[0, 0, 1], [1, 1, 1]], color: "#f97316" },  // L
            { shape: [[1, 1], [1, 1]], color: "#eab308" },        // O
            { shape: [[0, 1, 1], [1, 1, 0]], color: "#22c55e" },  // S
            { shape: [[0, 1, 0], [1, 1, 1]], color: "#a855f7" },  // T
            { shape: [[1, 1, 0], [0, 1, 1]], color: "#ef4444" }   // Z
        ];

        function createPieceInstance(p) {
            return {
                shape: p.shape.map(row => [...row]),
                originalShape: p.shape.map(row => [...row]),
                color: p.color,
                x: Math.floor(COLS / 2) - Math.ceil(p.shape[0].length / 2),
                y: 0
            };
        }

        function getRandomPiece() {
            const p = PIECES[Math.floor(Math.random() * PIECES.length)];
            return createPieceInstance(p);
        }

        let current = getRandomPiece();
        let nextPiece = getRandomPiece();
        let holdPiece = null;
        let canHold = true;

        function collides(piece, offsetX, offsetY, newShape) {
            const s = newShape || piece.shape;
            for (let r = 0; r < s.length; r++) {
                for (let c = 0; c < s[r].length; c++) {
                    if (s[r][c]) {
                        const targetX = piece.x + c + offsetX;
                        const targetY = piece.y + r + offsetY;

                        if (targetX < 0 || targetX >= COLS || targetY >= ROWS) return true;
                        if (targetY >= 0 && board[targetY][targetX]) return true;
                    }
                }
            }
            return false;
        }

        function rotateMatrix(matrix) {
            const rows = matrix.length;
            const cols = matrix[0].length;
            const rotated = Array.from({ length: cols }, () => Array(rows).fill(0));
            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    rotated[c][rows - 1 - r] = matrix[r][c];
                }
            }
            return rotated;
        }

        function spawnLineParticles(rowY, colors, isTetris = false) {
            const countPerCell = isTetris ? 4 : 2;
            for (let c = 0; c < COLS; c++) {
                const color = colors[c] || "#ffffff";
                const cellCenterX = OFFSET_X + c * BLOCK + BLOCK / 2;
                const cellCenterY = OFFSET_Y + rowY * BLOCK + BLOCK / 2;

                for (let i = 0; i < countPerCell; i++) {
                    particles.push({
                        x: cellCenterX + (Math.random() - 0.5) * BLOCK,
                        y: cellCenterY + (Math.random() - 0.5) * BLOCK,
                        vx: (Math.random() - 0.5) * (isTetris ? 8 : 5),
                        vy: (Math.random() - 0.7) * (isTetris ? 7 : 4) - 1.5,
                        size: Math.random() * 4 + 2,
                        color: color,
                        alpha: 1.0,
                        decay: Math.random() * 0.04 + 0.035
                    });
                }
            }
        }

        function triggerTetrisAnimation() {
            shakeFrames = 18;
            shakeIntensity = 8;

            shockwaves.push({
                x: OFFSET_X + BOARD_W / 2,
                y: OFFSET_Y + BOARD_H / 2,
                radius: 10,
                maxRadius: BOARD_W * 0.7,
                alpha: 1.0,
                color: "#38bdf8"
            });

            tetrisBanners.push({
                scale: 0.3,
                targetScale: 1.3,
                alpha: 1.0,
                y: OFFSET_Y + BOARD_H / 2
            });

            const centerX = OFFSET_X + BOARD_W / 2;
            const centerY = OFFSET_Y + BOARD_H / 2;
            const popColors = ["#00e5ff", "#facc15", "#f43f5e", "#a855f7", "#ffffff"];

            for (let i = 0; i < 45; i++) {
                const angle = Math.random() * Math.PI * 2;
                const speed = Math.random() * 8 + 3;
                particles.push({
                    x: centerX,
                    y: centerY,
                    vx: Math.cos(angle) * speed,
                    vy: Math.sin(angle) * speed,
                    size: Math.random() * 5 + 3,
                    color: popColors[Math.floor(Math.random() * popColors.length)],
                    alpha: 1.0,
                    decay: Math.random() * 0.03 + 0.02
                });
            }
        }

        function lockPiece() {
            for (let r = 0; r < current.shape.length; r++) {
                for (let c = 0; c < current.shape[r].length; c++) {
                    if (current.shape[r][c]) {
                        if (current.y + r <= 0) {
                            gameOver = true;
                            return;
                        }
                        board[current.y + r][current.x + c] = current.color;
                    }
                }
            }

            let linesCleared = 0;
            for (let r = ROWS - 1; r >= 0; r--) {
                if (board[r].every(cell => cell !== 0)) {
                    const rowColors = [...board[r]];
                    spawnLineParticles(r, rowColors, linesCleared === 3);

                    lineFlashes.push({
                        y: r,
                        alpha: 1.0
                    });

                    board.splice(r, 1);
                    board.unshift(Array(COLS).fill(0));
                    linesCleared++;
                    r++;
                }
            }

            if (linesCleared > 0) {
                const points = [0, 100, 300, 500, 800];
                score += (points[linesCleared] || 800) * level;
                linesClearedTotal += linesCleared;
                level = Math.floor(linesClearedTotal / 10) + 1;

                if (linesCleared === 4) {
                    triggerTetrisAnimation();
                } else {
                    shakeFrames = 6;
                    shakeIntensity = 3;
                }
            }

            canHold = true;
            current = nextPiece;
            nextPiece = getRandomPiece();

            if (collides(current, 0, 0)) {
                gameOver = true;
            }
        }

        function triggerHold() {
            if (!canHold || gameOver) return;

            const pieceToHold = {
                shape: current.originalShape.map(row => [...row]),
                originalShape: current.originalShape.map(row => [...row]),
                color: current.color
            };

            if (holdPiece === null) {
                holdPiece = pieceToHold;
                current = nextPiece;
                nextPiece = getRandomPiece();
            } else {
                const temp = holdPiece;
                holdPiece = pieceToHold;
                current = {
                    shape: temp.originalShape.map(row => [...row]),
                    originalShape: temp.originalShape.map(row => [...row]),
                    color: temp.color,
                    x: Math.floor(COLS / 2) - Math.ceil(temp.originalShape[0].length / 2),
                    y: 0
                };
            }

            canHold = false;

            if (collides(current, 0, 0)) {
                gameOver = true;
            }
        }

        function drawRoundedRect(x, y, w, h, r) {
            ctx.beginPath();
            ctx.moveTo(x + r, y);
            ctx.lineTo(x + w - r, y);
            ctx.quadraticCurveTo(x + w, y, x + w, y + r);
            ctx.lineTo(x + w, y + h - r);
            ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
            ctx.lineTo(x + r, y + h);
            ctx.quadraticCurveTo(x, y + h, x, y + h - r);
            ctx.lineTo(x, y + r);
            ctx.quadraticCurveTo(x, y, x + r, y);
            ctx.closePath();
        }

        function drawBlock(x, y, color, basePx = OFFSET_X, basePy = OFFSET_Y, size = BLOCK) {
            const px = basePx + x * size;
            const py = basePy + y * size;
            const bR = Math.max(3, Math.floor(size * 0.22));

            ctx.save();
            drawRoundedRect(px + 1, py + 1, size - 2, size - 2, bR);
            ctx.fillStyle = color;
            ctx.fill();

            ctx.fillStyle = "rgba(0, 0, 0, 0.3)";
            ctx.fillRect(px + 1, py + size - Math.max(3, Math.floor(size * 0.16)), size - 2, Math.floor(size * 0.16));
            ctx.fillRect(px + size - Math.max(3, Math.floor(size * 0.16)), py + 1, Math.floor(size * 0.16), size - 2);

            drawRoundedRect(px + 2, py + 2, size - 4, Math.floor(size * 0.42), Math.max(2, bR - 1));
            const grad = ctx.createLinearGradient(px, py + 2, px, py + 2 + size * 0.42);
            grad.addColorStop(0, "rgba(255, 255, 255, 0.65)");
            grad.addColorStop(1, "rgba(255, 255, 255, 0.05)");
            ctx.fillStyle = grad;
            ctx.fill();

            drawRoundedRect(px + 1, py + 1, size - 2, size - 2, bR);
            ctx.strokeStyle = "rgba(255, 255, 255, 0.3)";
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.restore();
        }

        function drawGhost() {
            let ghostY = current.y;
            while (!collides(current, 0, ghostY - current.y + 1)) {
                ghostY++;
            }
            if (ghostY === current.y) return;

            ctx.save();
            ctx.globalAlpha = 0.3;
            for (let r = 0; r < current.shape.length; r++) {
                for (let c = 0; c < current.shape[r].length; c++) {
                    if (current.shape[r][c]) {
                        const px = OFFSET_X + (current.x + c) * BLOCK;
                        const py = OFFSET_Y + (ghostY + r) * BLOCK;
                        drawRoundedRect(px + 2, py + 2, BLOCK - 4, BLOCK - 4, Math.max(2, BLOCK * 0.2));
                        ctx.strokeStyle = current.color;
                        ctx.lineWidth = 2;
                        ctx.stroke();
                    }
                }
            }
            ctx.restore();
        }

        function drawPanelCard(x, y, w, h, title = "") {
            ctx.save();
            drawRoundedRect(x, y, w, h, 16);
            ctx.fillStyle = "rgba(12, 18, 32, 0.9)";
            ctx.fill();

            ctx.strokeStyle = "rgba(56, 189, 248, 0.35)";
            ctx.lineWidth = 1.8;
            ctx.stroke();

            if (title) {
                ctx.font = "bold 12px monospace";
                ctx.fillStyle = "#38bdf8";
                ctx.textAlign = "center";
                ctx.fillText(title, x + w / 2, y + 20);
            }
            ctx.restore();
        }

        function drawMiniPiece(cardX, cardY, cardW, cardH, piece, isDim = false) {
            if (!piece) return;

            const previewBlock = Math.max(14, Math.floor(BLOCK * 0.65));
            const shape = piece.shape;
            const shapeWidth = shape[0].length * previewBlock;
            const shapeHeight = shape.length * previewBlock;

            const startX = cardX + (cardW - shapeWidth) / 2;
            const startY = cardY + 22 + (cardH - 22 - shapeHeight) / 2;

            ctx.save();
            if (isDim) ctx.globalAlpha = 0.3;
            for (let r = 0; r < shape.length; r++) {
                for (let c = 0; c < shape[r].length; c++) {
                    if (shape[r][c]) {
                        drawBlock(c, r, piece.color, startX, startY, previewBlock);
                    }
                }
            }
            ctx.restore();
        }

        function updateAndRenderEffects() {
            for (let i = lineFlashes.length - 1; i >= 0; i--) {
                const flash = lineFlashes[i];
                ctx.save();
                ctx.fillStyle = `rgba(255, 255, 255, ${flash.alpha})`;
                ctx.fillRect(OFFSET_X, OFFSET_Y + flash.y * BLOCK, BOARD_W, BLOCK);
                ctx.restore();

                flash.alpha -= 0.16;
                if (flash.alpha <= 0) lineFlashes.splice(i, 1);
            }

            for (let i = shockwaves.length - 1; i >= 0; i--) {
                const sw = shockwaves[i];
                sw.radius += 8;
                sw.alpha -= 0.05;

                if (sw.alpha <= 0 || sw.radius >= sw.maxRadius) {
                    shockwaves.splice(i, 1);
                    continue;
                }

                ctx.save();
                ctx.beginPath();
                ctx.arc(sw.x, sw.y, sw.radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(56, 189, 248, ${Math.max(0, sw.alpha)})`;
                ctx.lineWidth = 3;
                ctx.stroke();
                ctx.restore();
            }

            for (let i = particles.length - 1; i >= 0; i--) {
                const p = particles[i];
                p.x += p.vx;
                p.y += p.vy;
                p.vy += 0.25;
                p.alpha -= p.decay;

                if (p.alpha <= 0 || p.y > canvas.height || p.x < 0 || p.x > canvas.width) {
                    particles.splice(i, 1);
                    continue;
                }

                ctx.save();
                ctx.globalAlpha = Math.max(0, p.alpha);
                ctx.fillStyle = p.color;
                ctx.fillRect(p.x, p.y, p.size, p.size);
                ctx.restore();
            }

            for (let i = tetrisBanners.length - 1; i >= 0; i--) {
                const b = tetrisBanners[i];
                b.scale += (b.targetScale - b.scale) * 0.16;
                b.y -= 0.7;
                b.alpha -= 0.02;

                if (b.alpha <= 0) {
                    tetrisBanners.splice(i, 1);
                    continue;
                }

                ctx.save();
                ctx.globalAlpha = Math.max(0, b.alpha);
                ctx.translate(OFFSET_X + BOARD_W / 2, b.y);
                ctx.scale(b.scale, b.scale);

                ctx.font = `bold ${Math.floor(BLOCK * 1.3)}px monospace`;
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";

                ctx.lineWidth = 5;
                ctx.strokeStyle = "#0284c7";
                ctx.strokeText("TETRIS", 0, 0);

                ctx.fillStyle = "#facc15";
                ctx.fillText("TETRIS", 0, 0);

                ctx.restore();
            }
        }

        function drawBackground() {
            ctx.fillStyle = "#060913";
            ctx.fillRect(0, 0, canvas.width, canvas.height);

            ctx.save();
            for (let s of stars) {
                const a = s.alpha + Math.sin(Date.now() * s.pulseSpeed) * 0.15;
                ctx.globalAlpha = Math.max(0.1, Math.min(0.7, a));
                ctx.fillStyle = "#ffffff";
                ctx.fillRect(s.x, s.y, s.size, s.size);
            }
            ctx.restore();
        }

        function render() {
            ctx.globalCompositeOperation = "source-over";
            ctx.globalAlpha = 1.0;
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            ctx.save();

            if (shakeFrames > 0) {
                const sx = (Math.random() - 0.5) * shakeIntensity;
                const sy = (Math.random() - 0.5) * shakeIntensity;
                ctx.translate(sx, sy);
                shakeFrames--;
            }

            drawBackground();

            // Título superior
            ctx.font = `bold ${Math.max(22, Math.floor(canvas.height * 0.038))}px monospace`;
            ctx.textAlign = "center";
            ctx.fillStyle = "#38bdf8";
            ctx.fillText("T E T R I S", canvas.width / 2, Math.max(32, OFFSET_Y - 20));

            // Marco central
            drawRoundedRect(OFFSET_X - 8, OFFSET_Y - 8, BOARD_W + 16, BOARD_H + 16, 18);
            ctx.fillStyle = "rgba(10, 15, 28, 0.96)";
            ctx.fill();
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = 3;
            ctx.stroke();

            // Cuadrícula
            ctx.strokeStyle = "rgba(255, 255, 255, 0.04)";
            ctx.lineWidth = 1;
            for (let c = 1; c < COLS; c++) {
                ctx.beginPath();
                ctx.moveTo(OFFSET_X + c * BLOCK, OFFSET_Y);
                ctx.lineTo(OFFSET_X + c * BLOCK, OFFSET_Y + BOARD_H);
                ctx.stroke();
            }
            for (let r = 1; r < ROWS; r++) {
                ctx.beginPath();
                ctx.moveTo(OFFSET_X, OFFSET_Y + r * BLOCK);
                ctx.lineTo(OFFSET_X + BOARD_W, OFFSET_Y + r * BLOCK);
                ctx.stroke();
            }

            // Bloques asentados
            for (let r = 0; r < ROWS; r++) {
                for (let c = 0; c < COLS; c++) {
                    if (board[r][c]) {
                        drawBlock(c, r, board[r][c]);
                    }
                }
            }

            // Pieza fantasma y activa
            if (!gameOver && current) {
                drawGhost();
                for (let r = 0; r < current.shape.length; r++) {
                    for (let c = 0; c < current.shape[r].length; c++) {
                        if (current.shape[r][c]) {
                            drawBlock(current.x + c, current.y + r, current.color);
                        }
                    }
                }
            }

            updateAndRenderEffects();

            // Dimensionamiento simétrico de paneles laterales
            const sideCardW = Math.min(220, Math.max(140, Math.floor((canvas.width - BOARD_W) * 0.32)));
            const leftX = OFFSET_X - sideCardW - Math.max(16, Math.floor(canvas.width * 0.02));
            const rightX = OFFSET_X + BOARD_W + Math.max(16, Math.floor(canvas.width * 0.02));

            // PANEL IZQUIERDO: HOLD & STATS
            const holdCardH = Math.min(125, Math.max(85, Math.floor(BOARD_H * 0.22)));
            drawPanelCard(leftX, OFFSET_Y, sideCardW, holdCardH, "HOLD (X)");
            drawMiniPiece(leftX, OFFSET_Y, sideCardW, holdCardH, holdPiece, !canHold);

            const statsY = OFFSET_Y + holdCardH + 16;
            const statsH = Math.min(190, Math.max(130, Math.floor(BOARD_H * 0.35)));
            drawPanelCard(leftX, statsY, sideCardW, statsH, "STATS");
            ctx.font = "bold 11px monospace";
            ctx.fillStyle = "#94a3b8";
            ctx.textAlign = "center";

            ctx.fillText("LEVEL", leftX + sideCardW / 2, statsY + 45);
            ctx.font = "bold 22px monospace";
            ctx.fillStyle = "#ffffff";
            ctx.fillText(String(level), leftX + sideCardW / 2, statsY + 75);

            ctx.font = "bold 11px monospace";
            ctx.fillStyle = "#94a3b8";
            ctx.fillText("LINES", leftX + sideCardW / 2, statsY + 115);
            ctx.font = "bold 22px monospace";
            ctx.fillStyle = "#38bdf8";
            ctx.fillText(String(linesClearedTotal), leftX + sideCardW / 2, statsY + 145);

            // TEXTO INFERIOR: RETROGAMES LAB (Sin caja ni dibujos, tipografía nítida)
            ctx.save();
            ctx.textAlign = "center";
            ctx.fillStyle = "#00e5ff";
            ctx.shadowColor = "#00e5ff";
            ctx.shadowBlur = 10;

            const brandY = OFFSET_Y + BOARD_H - 36;
            const brandFontSize1 = Math.max(13, Math.floor(sideCardW * 0.095));
            ctx.font = `bold ${brandFontSize1}px monospace`;
            ctx.fillText("RetroGames", leftX + sideCardW / 2, brandY);

            const brandFontSize2 = Math.max(16, Math.floor(sideCardW * 0.13));
            ctx.font = `bold ${brandFontSize2}px monospace`;
            ctx.fillText(". . Lab . .", leftX + sideCardW / 2, brandY + 24);
            ctx.restore();

            // PANEL DERECHO: NEXT & SCORE & CONTROLES
            const nextCardH = holdCardH;
            drawPanelCard(rightX, OFFSET_Y, sideCardW, nextCardH, "NEXT");
            drawMiniPiece(rightX, OFFSET_Y, sideCardW, nextCardH, nextPiece);

            const scoreY = OFFSET_Y + nextCardH + 16;
            const scoreH = Math.min(110, Math.max(80, Math.floor(BOARD_H * 0.18)));
            drawPanelCard(rightX, scoreY, sideCardW, scoreH, "SCORE");
            ctx.font = "bold 22px monospace";
            ctx.fillStyle = "#facc15";
            ctx.textAlign = "center";
            ctx.fillText(String(score), rightX + sideCardW / 2, scoreY + scoreH / 2 + 10);

            const ctrlY = scoreY + scoreH + 16;
            const ctrlH = Math.max(160, BOARD_H - (ctrlY - OFFSET_Y));
            drawPanelCard(rightX, ctrlY, sideCardW, ctrlH, "CONTROLES");
            ctx.font = "11px monospace";
            ctx.fillStyle = "#cbd5e1";
            ctx.textAlign = "left";
            const ctrlX = rightX + Math.max(12, Math.floor(sideCardW * 0.1));
            const rowStep = Math.floor(ctrlH / 7.5);

            ctx.fillText("D-PAD : Mover", ctrlX, ctrlY + rowStep * 1.5);
            ctx.fillText("(^)   : Drop Max", ctrlX, ctrlY + rowStep * 2.5);
            ctx.fillText("(v)   : Suave", ctrlX, ctrlY + rowStep * 3.5);
            ctx.fillText("(A)   : Rotar", ctrlX, ctrlY + rowStep * 4.5);
            ctx.fillText("(X)   : Guardar", ctrlX, ctrlY + rowStep * 5.5);
            ctx.fillText("(B)   : Salir", ctrlX, ctrlY + rowStep * 6.5);

            if (gameOver) {
                drawRoundedRect(OFFSET_X + 15, OFFSET_Y + BOARD_H * 0.36, BOARD_W - 30, 130, 18);
                ctx.fillStyle = "rgba(10, 15, 28, 0.96)";
                ctx.fill();
                ctx.strokeStyle = "#ef4444";
                ctx.lineWidth = 2.5;
                ctx.stroke();

                ctx.fillStyle = "#ef4444";
                ctx.font = `bold ${Math.max(20, Math.floor(BLOCK * 0.95))}px monospace`;
                ctx.textAlign = "center";
                ctx.fillText("GAME OVER", OFFSET_X + BOARD_W / 2, OFFSET_Y + BOARD_H * 0.36 + 55);

                ctx.font = "12px monospace";
                ctx.fillStyle = "#ffffff";
                ctx.fillText("Presiona (B) para salir", OFFSET_X + BOARD_W / 2, OFFSET_Y + BOARD_H * 0.36 + 92);
            }

            ctx.restore();
        }

        const DAS = 150;
        const ARR = 40;

        const keys = {
            left: { isDown: false, pressTime: 0, lastRepeat: 0 },
            right: { isDown: false, pressTime: 0, lastRepeat: 0 },
            down: { isDown: false, pressTime: 0, lastRepeat: 0 }
        };

        function handleContinuousInput(now) {
            if (gameOver) return;

            let dir = 0;
            let active = null;

            if (keys.left.isDown && !keys.right.isDown) {
                dir = -1;
                active = keys.left;
            } else if (keys.right.isDown && !keys.left.isDown) {
                dir = 1;
                active = keys.right;
            }

            if (dir !== 0 && active) {
                if (now - active.pressTime >= DAS) {
                    if (now - active.lastRepeat >= ARR) {
                        if (!collides(current, dir, 0)) {
                            current.x += dir;
                        }
                        active.lastRepeat = now;
                    }
                }
            }

            if (keys.down.isDown) {
                if (now - keys.down.pressTime >= 60 && now - keys.down.lastRepeat >= 45) {
                    if (!collides(current, 0, 1)) {
                        current.y++;
                        score += 1;
                    } else {
                        lockPiece();
                    }
                    lastDrop = now;
                    keys.down.lastRepeat = now;
                }
            }
        }

        let lastDrop = Date.now();

        const gameLoop = () => {
            const now = Date.now();
            const dropInterval = Math.max(120, 650 - (level - 1) * 45);

            handleContinuousInput(now);

            if (!gameOver && now - lastDrop > dropInterval) {
                if (!collides(current, 0, 1)) {
                    current.y++;
                } else {
                    lockPiece();
                }
                lastDrop = now;
            }

            render();
        };

        window._tetrisInterval = setInterval(gameLoop, 16);
        await log("Tetris con firma RetroGames Lab activa.");

        await new Promise((resolve) => {
            let isCleaned = false;

            const stopGame = () => {
                if (isCleaned) return;
                isCleaned = true;

                window.removeEventListener("resize", resizeGame);

                if (window._tetrisInterval) {
                    clearInterval(window._tetrisInterval);
                    window._tetrisInterval = null;
                }

                if (typeof engine !== "undefined" && typeof engine.off === "function") {
                    try { engine.off("InputAction", inputHandler); } catch (e) {}
                }

                const c = document.getElementById(id);
                if (c) c.remove();

                log("Tetris finalizado.");
                resolve();
            };

            const inputHandler = function(action) {
                const val = arguments[1];

                if (action === "cancel" && val === 0) {
                    stopGame();
                    return;
                }

                if (gameOver || isCleaned) return;

                const now = Date.now();

                if (action === "nav-left") {
                    if (val === 0) {
                        keys.left.isDown = true;
                        keys.left.pressTime = now;
                        keys.left.lastRepeat = now;
                        keys.right.isDown = false;
                        if (!collides(current, -1, 0)) current.x--;
                    } else if (val === 1) {
                        keys.left.isDown = false;
                    }
                } else if (action === "nav-right") {
                    if (val === 0) {
                        keys.right.isDown = true;
                        keys.right.pressTime = now;
                        keys.right.lastRepeat = now;
                        keys.left.isDown = false;
                        if (!collides(current, 1, 0)) current.x++;
                    } else if (val === 1) {
                        keys.right.isDown = false;
                    }
                } else if (action === "nav-down") {
                    if (val === 0) {
                        keys.down.isDown = true;
                        keys.down.pressTime = now;
                        keys.down.lastRepeat = now;
                        if (!collides(current, 0, 1)) {
                            current.y++;
                            score += 1;
                        } else {
                            lockPiece();
                        }
                        lastDrop = now;
                    } else if (val === 1) {
                        keys.down.isDown = false;
                    }
                } else if ((action === "nav-previous" || action === "nav-up") && val === 0) {
                    while (!collides(current, 0, 1)) {
                        current.y++;
                        score += 2;
                    }
                    lockPiece();
                    lastDrop = Date.now();
                } else if (action === "accept" && val === 0) {
                    const rotated = rotateMatrix(current.shape);
                    if (!collides(current, 0, 0, rotated)) {
                        current.shape = rotated;
                    } else if (!collides(current, -1, 0, rotated)) {
                        current.x--;
                        current.shape = rotated;
                    } else if (!collides(current, 1, 0, rotated)) {
                        current.x++;
                        current.shape = rotated;
                    }
                } else if ((action === "shell-action-1" || action === "shell-action-2") && val === 0) {
                    triggerHold();
                }
            };

            if (typeof engine !== "undefined" && typeof engine.on === "function") {
                engine.on("InputAction", inputHandler);
            }
        });

    } catch (e) {
        await log("Error en Tetris: " + e.message);
    }
})();