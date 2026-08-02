# A Three-Theorem Basis for Prompt Distillation

本文将 Prompt KD 的理论解释整理为三个定理：

1. 如果回答行为依赖任务，那么任务信息必须存在于读取 prompt 后形成的状态中；
2. 如果 prompt predictive distributions 在任务相关方向上具有足够的 Fisher curvature，那么 prompt loss 控制 prompt-state discrepancy；
3. 如果 answer decoder 对状态扰动是 Lipschitz 的，那么 answer-side KD loss 被 prompt-side KD loss 的线性函数所上界。

第三个结论说明 prompt loss 下降会收紧 answer loss 的理论上界。仅凭这个上界，不能无条件推出实际 answer loss 在每一个优化步骤都严格下降；文末给出能够严格推出下降的附加条件。

## 1. Notation and the Difference Between `z` and `f`

令 \(T\) 表示任务变量，\(C\) 表示与任务无关的实例内容，\(X\) 表示完整 prompt，\(Y\) 表示 teacher-forced answer。

student 和任务 \(\tau\) 的 teacher 读取 prompt 后分别形成

\[
H_\theta(X), \qquad H_T^\tau(X).
\]

当 teacher 和 student 宽度不同时，使用固定 alignment map \(A_S,A_T\) 将其映射到共同且 decoder-compatible 的状态空间：

\[
r_\theta(X)=A_SH_\theta(X),
\qquad
r_T^\tau(X)=A_TH_T^\tau(X).
\]

这里 \(r\) 表示与后续 answer decoding 有关的对齐后 prompt state。它可以由全层 prompt hidden states、prompt KV states，或其固定压缩表示构成。

对第 \(j\) 个 answer position，定义

\[
f_{\theta,j}(r,y_{<j})\in\mathbb{R}^{|\mathcal V|}
\]

为一个**函数**：固定 student decoder 参数和 teacher-forced prefix \(y_{<j}\)，当提供的 prompt state 为 \(r\) 时，它输出 answer logits。

相应地，

\[
z_{\theta,j}
=f_{\theta,j}(r_\theta(X),y_{<j})
\]

是 student 正常前向传播得到的**实际 logit 向量**，而

\[
\widetilde z_{\theta,j}^{\,\tau}
=f_{\theta,j}(r_T^\tau(X),y_{<j})
\]

是把同任务 teacher prompt state 注入 student decoder 后得到的**反事实 patched logits**。最后，

\[
z_{T,j}^\tau
\]

是 teacher 使用自己的 prompt state 和 decoder 得到的 logits。因此，\(f\) 是从 prompt state 到 logits 的 decoder 映射，而 \(z\) 和 \(\widetilde z\) 是该映射在特定输入上的取值。

定义任务 \(\tau\) 上的 prompt- 和 answer-side reverse-KL losses：

\[
L_X^\tau(\theta)
=\mathbb E_{x,i}
D_{\mathrm{RKL}}(p_{\theta,i}\Vert p_{T,i}^\tau),
\]

\[
L_Y^\tau(\theta)
=\mathbb E_{x,y,j}
D_{\mathrm{RKL}}
\left(
\operatorname{softmax}(z_{\theta,j})
\Vert
\operatorname{softmax}(z_{T,j}^\tau)
\right).
\]

所有期望均采用与实际 loss 相同的样本、位置和 token normalization。

## 2. Theorem 1: Prompt States Must Contain Task Information

### Assumptions

**Assumption 1.1 (task-dependent answer behavior).** 在控制实例内容 \(C\) 后，期望的回答行为仍然依赖任务：

\[
I(T;Y\mid C)>0.
\]

**Assumption 1.2 (prompt-state mediation).** 模型读取完 prompt 后，任务对后续回答的影响由 prompt state \(H\) 完全中介：

\[
P(Y\mid T,C,H)=P(Y\mid C,H).
\]

等价地，给定 \(C\) 后存在条件 Markov 链

\[
T\longrightarrow H\longrightarrow Y.
\]

对于 causal Transformer，这一假设对应如下事实：prompt 处理结束后，后续 token generation 只能通过已有 prompt hidden/KV state、共享模型参数以及先前生成的 tokens 使用 prompt 中的任务信息。

### Theorem 1

在 Assumptions 1.1--1.2 下，prompt state 必须包含任务信息：

\[
I(T;H\mid C)>0.
\]

### Proof

由 Assumption 1.2，给定 \(C\) 后有 Markov 链 \(T\to H\to Y\)。条件数据处理不等式给出

\[
I(T;Y\mid C)
\le
I(T;H\mid C).
\]

再由 Assumption 1.1，左侧严格大于零，因此

\[
I(T;H\mid C)
\ge I(T;Y\mid C)>0.
\]

证毕。

### What Theorem 1 Does and Does Not Prove

Theorem 1 使用的是**条件 Markov 链和数据处理不等式**。它证明 prompt state 中必然存在对 answer behavior 有用的任务信息，但不自动证明：

- 该信息一定组成线性子空间；
- prompt-side logits 一定能观测该信息；
- Prompt KD 一定能减小该信息与 teacher 之间的差异。

后续将 \(r=AH\) 称为 task-relevant state，是额外的表示假设。最保守时可以令 \(r=H\)；使用低维或线性 task subspace 时，需要实验或结构假设支持该选择。

## 3. Theorem 2: Prompt-State Observability

Theorem 1 证明任务信息必须存在于 prompt state，但没有证明 prompt-side predictive distributions 能观测该信息。Theorem 2 给出一组明确的充分条件，将这一 observability 结论单独证明出来。

### Assumptions

**Assumption 2.1 (common aligned prompt-state coordinate).** 对每个被蒸馏的 prompt position \(i\)，固定 alignment maps 将 student 和 teacher states 映射到同一欧氏空间，分别记为 \(r_{\theta,i}(x)\) 和 \(r_{T,i}^\tau(x)\)。

**Assumption 2.2 (observable/null decomposition).** 存在固定正交投影 \(\Pi_U\)，使 state difference 可以分解为

\[
r_{\theta,i}-r_{T,i}^\tau
=
\delta_{U,i}+\delta_{\perp,i},
\qquad
\delta_{U,i}=\Pi_U(r_{\theta,i}-r_{T,i}^\tau),
\]

其中 \(U\) 包含希望由 prompt logits 观测的 task-relevant directions，而不可观测部分满足

\[
\mathbb E_{x,i}\|\delta_{\perp,i}\|_2^2
\le
\varepsilon_{\mathrm{null}}^\tau.
\]

**Assumption 2.3 (shared affine prompt readout).** 在对齐后的坐标中，第 \(i\) 个 prompt position 的 student 和 teacher logits 分别可写为

\[
u_{\theta,i}=W_i r_{\theta,i}+b_i,
\qquad
u_{T,i}^\tau=W_i r_{T,i}^\tau+b_i,
\]

即二者在 task-relevant state 上共享同一个局部 affine readout。对于不同宽度的模型，这要求 alignment maps 吸收 readout-coordinate difference。Theorem 2 的当前形式要求该关系精确成立；若只能近似成立，必须另外引入 readout-approximation residual，不能自动将其并入 state null residual。

**Assumption 2.4 (restricted Fisher lower bound).** 令

\[
u_i(s)=u_{\theta,i}+s(u_{T,i}^\tau-u_{\theta,i}),
\qquad s\in[0,1],
\]

并定义 softmax Fisher matrix

\[
C_i(s)
=
\operatorname{Diag}(p_i(s))-p_i(s)p_i(s)^\top,
\qquad
p_i(s)=\operatorname{softmax}(u_i(s)).
\]

假设沿该线段存在 \(\mu_\tau>0\)，使

\[
W_i^\top C_i(s)W_i
\succeq
\mu_\tau\Pi_U
\]

对所有相关样本、positions 和 \(s\in[0,1]\) 成立。

**Assumption 2.5 (consistent normalization).** \(L_X^\tau\) 与下面的 state discrepancy 使用相同的样本分布、prompt positions 和 token normalization。

### Theorem 2

定义完整的 aligned prompt-state discrepancy

\[
\mathcal E_H^\tau(\theta)
:=
\mathbb E_{x,i}
\|r_{\theta,i}(x)-r_{T,i}^\tau(x)\|_2^2.
\]

在 Assumptions 2.1--2.5 下，

\[
\boxed{
\mathcal E_H^\tau(\theta)
\le
\frac{2}{\mu_\tau}L_X^\tau(\theta)
+\varepsilon_{\mathrm{null}}^\tau
}.
\]

### Proof

令 \(A(u)=\log\sum_v\exp(u_v)\)。softmax reverse KL 是 log-sum-exp 的 Bregman divergence：

\[
D_{\mathrm{RKL}}
(\operatorname{softmax}(u_{\theta,i})
\Vert
\operatorname{softmax}(u_{T,i}^\tau))
=
D_A(u_{T,i}^\tau,u_{\theta,i}).
\]

由 Bregman divergence 的积分二阶展开，

\[
\begin{aligned}
D_A(u_{T,i}^\tau,u_{\theta,i})
={}&
\int_0^1(1-s)
(u_{T,i}^\tau-u_{\theta,i})^\top
C_i(s)
(u_{T,i}^\tau-u_{\theta,i})\,ds.
\end{aligned}
\]

由 Assumption 2.3，

\[
u_{T,i}^\tau-u_{\theta,i}
=
W_i(r_{T,i}^\tau-r_{\theta,i}).
\]

再由 Assumption 2.4，

\[
D_{\mathrm{RKL}}(p_{\theta,i}\Vert p_{T,i}^\tau)
\ge
\frac{\mu_\tau}{2}
\|\Pi_U(r_{\theta,i}-r_{T,i}^\tau)\|_2^2.
\]

按照 Assumption 2.5 求平均可得

\[
\mathbb E_{x,i}\|\delta_{U,i}\|_2^2
\le
\frac{2}{\mu_\tau}L_X^\tau(\theta).
\]

由于 \(\delta_{U,i}\perp\delta_{\perp,i}\)，结合 Assumption 2.2，

\[
\begin{aligned}
\mathcal E_H^\tau(\theta)
&=
\mathbb E_{x,i}
\bigl(\|\delta_{U,i}\|_2^2+\|\delta_{\perp,i}\|_2^2\bigr)\\
&\le
\frac{2}{\mu_\tau}L_X^\tau(\theta)
+\varepsilon_{\mathrm{null}}^\tau.
\end{aligned}
\]

证毕。

### Proof Method and Scope

Theorem 2 使用的是 **reverse-KL 的 Bregman 二阶展开和 pullback Fisher 下界**。Fisher lower bound 保证 task-relevant state 的变化会在 prompt predictive distribution 中留下可观测变化；\(\varepsilon_{\mathrm{null}}^\tau\) 则保留无法由 prompt logits 识别的方向。

该定理不由 Theorem 1 自动推出。Theorem 1 证明任务信息存在；Theorem 2 额外刻画这些任务信息何时暴露在 prompt-side distributions 中。

## 4. Theorem 3: Prompt Loss Upper-Bounds Answer Loss

### Assumptions

**Assumption 3.1 (common decoder-compatible state coordinate).** Theorem 2 的 aligned states 可以组成 decoder-compatible prompt state \(r_\theta(X)\) 和 \(r_T^\tau(X)\)。student decoder 可以接收该空间中的相关状态作为 \(f_{\theta,j}\) 的输入。若 alignment 只允许比较距离、不能将 teacher state 注入 student decoder，则 patched logits \(\widetilde z_{\theta,j}^{\,\tau}\) 没有定义，Theorem 3 不直接适用。

**Assumption 3.2 (applicability of Theorem 2).** Assumptions 2.1--2.5 成立，因此由 Theorem 2 可得

\[
\mathcal E_H^\tau(\theta)
\le
a_\tau L_X^\tau(\theta)
+\varepsilon_{\mathrm{null}}^\tau,
\qquad
a_\tau:=\frac{2}{\mu_\tau}.
\]

这里对 stacked/aggregated prompt state 使用与 Theorem 2 一致的 normalized norm。

**Assumption 3.3 (decoder Lipschitzness).** 对 teacher-forced answer prefix \(y_{<j}\)，student decoder 关于 prompt state 是局部 Lipschitz 的：

\[
\|f_{\theta,j}(r_1,y_{<j})
-f_{\theta,j}(r_2,y_{<j})\|_2
\le
L_\tau\|r_1-r_2\|_2.
\]

该不等式对连接 \(r_\theta(x)\) 和 \(r_T^\tau(x)\) 的状态区域成立。一个充分条件是相应 decoder Jacobian 的 operator norm 被 \(L_\tau\) 一致控制。

**Assumption 3.4 (uniformly bounded inference residual).** 在所分析的参数邻域内，存在固定常数 \(\varepsilon_{\mathrm{inf}}^\tau\ge0\)，使

\[
\mathbb E_{x,y,j}
\|\widetilde z_{\theta,j}^{\,\tau}-z_{T,j}^\tau\|_2^2
\le
\varepsilon_{\mathrm{inf}}^\tau.
\]

该项汇总不能由 prompt state matching 消除的 student/teacher decoder、模型容量和 answer-prefix processing 差异。

**Assumption 3.5 (shared output space).** teacher 和 student 使用相同 tokenizer、output vocabulary 和 unit-temperature softmax，使 reverse KL 与 logits distance 可以直接比较。

### Theorem 3

在 Assumptions 3.1--3.5 下，

\[
\boxed{
L_Y^\tau(\theta)
\le
\frac{a_\tau L_\tau^2}{2}L_X^\tau(\theta)
+
\frac{
L_\tau^2\varepsilon_{\mathrm{null}}^\tau
+\varepsilon_{\mathrm{inf}}^\tau
}{2}
}.
\]

### Proof

首先在 **logit space** 中进行分解：

\[
\begin{aligned}
z_{\theta,j}-z_{T,j}^\tau
={}&
\underbrace{
f_{\theta,j}(r_\theta,y_{<j})
-f_{\theta,j}(r_T^\tau,y_{<j})
}_{\text{prompt-state-mediated discrepancy}}\\
&+
\underbrace{
f_{\theta,j}(r_T^\tau,y_{<j})
-z_{T,j}^\tau
}_{\text{inference/decoder residual}}.
\end{aligned}
\]

注意这里分解的是 logits difference，而不是直接将 KL loss 拆成两个可加项。由

\[
\|a+b\|_2^2\le2\|a\|_2^2+2\|b\|_2^2
\]

以及 Assumptions 3.3--3.4，

\[
\mathbb E_{x,y,j}
\|z_{\theta,j}-z_{T,j}^\tau\|_2^2
\le
2L_\tau^2\mathcal E_H^\tau(\theta)
+2\varepsilon_{\mathrm{inf}}^\tau.
\]

对任意 logits \(u,v\)，unit-temperature softmax 满足

\[
D_{\mathrm{RKL}}
(\operatorname{softmax}(u)\Vert\operatorname{softmax}(v))
\le
\frac14\|u-v\|_2^2.
\]

因此

\[
L_Y^\tau(\theta)
\le
\frac{L_\tau^2}{2}\mathcal E_H^\tau(\theta)
+\frac{\varepsilon_{\mathrm{inf}}^\tau}{2}.
\]

最后代入 Assumption 3.2 即得结论。证毕。

### Interpretation of the Two Terms

Theorem 3 将 answer-logit discrepancy 分成两部分：

1. **Prompt-state-mediated discrepancy**：由 student 与同任务 teacher 的 prompt states 不一致造成，并由 Theorem 2 和 \(L_X^\tau\) 控制；
2. **Inference/decoder residual**：即使 prompt states 已对齐，student decoder 与 teacher decoder 之间仍然存在的误差，由 \(\varepsilon_{\mathrm{inf}}^\tau\) 表示。

因此，Prompt KD 通过减小 \(L_X^\tau\) 收紧第一部分的上界，但通常不能消除第二部分。

## 5. What Kind of Answer-Loss Descent Is Actually Proven?

定义 Theorem 3 给出的上界

\[
B_\tau(\theta)
:=
\frac{a_\tau L_\tau^2}{2}L_X^\tau(\theta)
+
\frac{
L_\tau^2\varepsilon_{\mathrm{null}}^\tau
+\varepsilon_{\mathrm{inf}}^\tau
}{2}.
\]

若 \(L_\tau,a_\tau,\varepsilon_{\mathrm{null}}^\tau,
\varepsilon_{\mathrm{inf}}^\tau\) 在一次局部比较中保持不变，则 Prompt KD 使 \(L_X^\tau\) 减少时，\(B_\tau\) 同步减少：

\[
B_\tau(\theta_{t+1})-B_\tau(\theta_t)
=
\frac{a_\tau L_\tau^2}{2}
\left(
L_X^\tau(\theta_{t+1})-L_X^\tau(\theta_t)
\right)<0.
\]

这证明的是 **answer-loss upper-bound descent**，而不是无条件的 actual answer-loss descent。要严格推出

\[
L_Y^\tau(\theta_{t+1})<L_Y^\tau(\theta_t),
\]

一个充分的可验证条件是

\[
B_\tau(\theta_{t+1})
<
L_Y^\tau(\theta_t).
\]

因为 Theorem 3 给出

\[
L_Y^\tau(\theta_{t+1})
\le B_\tau(\theta_{t+1})
<L_Y^\tau(\theta_t).
\]

若只知道 \(L_X^\tau\) 下降，而不知道上界是否足够紧，则不能排除 \(L_Y^\tau\) 在收紧后的上界内部发生局部波动。逐步单调下降需要额外的梯度对齐条件，或由实验直接验证。

## 6. Why Task Matching Matters

上述结论要求 \(L_X^\tau\)、\(r_T^\tau\) 和 \(L_Y^\tau\) 对应同一个任务和同一个数据分布。此时 Prompt KD 所逼近的 state reference 正是 answer loss 上界中使用的 reference。

若使用任务 \(s\ne\tau\) 训练，则 Prompt KD 控制的是

\[
\|r_\theta-r_T^s\|,
\]

而目标 answer loss 需要控制

\[
\|r_\theta-r_T^\tau\|.
\]

二者之间还会出现 teacher-state mismatch

\[
\Delta_{s,\tau}^2
=
\mathbb E\|r_T^s-r_T^\tau\|_2^2
\]

以及训练分布到目标分布的 distribution-shift residual。除非额外假设两个任务的 state targets 接近且 Prompt KD 的状态控制能够跨分布泛化，否则 mismatched-task Prompt KD 不具有 Theorem 3 的同任务 answer-loss bound。相关任务可能因 \(\Delta_{s,\tau}\) 较小而产生正迁移，因此理论上应表述为“task matching removes the state-target mismatch term”，而不是“训练任务与推理任务必须完全相同”。

## 7. Proof Roadmap

完整逻辑链为

\[
\text{task-dependent answer behavior}
\overset{\text{conditional DPI}}{\Longrightarrow}
\text{task information in prompt states},
\]

\[
\text{prompt-state observability}
\Longrightarrow
L_X^\tau
\text{ controls task-state discrepancy},
\]

\[
\text{decoder Lipschitzness and bounded inference residual}
\Longrightarrow
L_X^\tau
\text{ upper-bounds }L_Y^\tau.
\]

Theorem 1 解释任务信息为什么必须存在于 prompt state；Theorem 2 证明 prompt-side distributions 在何种条件下能够观测并控制该状态；Theorem 3 再将 state discrepancy 转换成 answer-side loss 上界。
