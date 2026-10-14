
import numpy as np
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

epochs = np.arange(0, 51)

# ========== 普通标准训练模型 ==========
clean_std = np.linspace(70, 91, 51) + np.random.normal(0,0.3,51)
robust_std = np.linspace(20, 32, 51) + np.random.normal(0,0.3,51)

# ========== 对抗训练模型 ==========
clean_at = np.linspace(68, 84, 51) + np.random.normal(0,0.3,51) # 干净性能下降
robust_at = np.linspace(45, 68, 51) + np.random.normal(0,0.3,51) # 鲁棒性大幅提升


fig, ax = plt.subplots(figsize=(7,5), dpi=300)

ax.plot(epochs, clean_std, color="#2E86AB", linewidth=2.2, label="标准训练 - 干净准确率")
ax.plot(epochs, robust_std, color="#F24236", linewidth=2.2, label="标准训练 - 鲁棒准确率")

ax.plot(epochs, clean_at, color="#2E86AB", linestyle='--', linewidth=2.2, label="对抗训练 - 干净准确率")
ax.plot(epochs, robust_at, color="#F24236", linestyle='--', linewidth=2.2, label="对抗训练 - 鲁棒准确率")

ax.set_xlabel("Epoch", fontsize=10)
ax.set_ylabel("准确率 (%)", fontsize=10)
ax.set_title("对抗训练的鲁棒性与干净泛化权衡", fontsize=11)
ax.set_ylim([10, 95])
ax.legend(fontsize=8.5)
ax.grid(alpha=0.25)

plt.tight_layout()
plt.savefig("adv_training_tradeoff.png", bbox_inches="tight", dpi=300)
plt.show()
