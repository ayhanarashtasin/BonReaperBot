# Strategy comparison (simulated)

> SIMULATED, unverified queue assumptions. Not real earnings.

- Coverage grade: **B: continuous capture, conclusions still need out-of-sample review**
- Code: `397ee5ddd9570374592baf4459af34218763dbee`
- Config hash: `7ee0a03e77c84fd606087a612694cb241219c8dc308ce789d4f28108ee93f667`
- SDK: `0.17.0`
- Fee schedule: `crypto_fees_v2:rate=0.07:exp=1:taker_only=True:shares_on_buy`
- Seed: `7`
- Input hash: `dc00dc0063102b794270a1eefafc3d409ca413a927878de515f09fb363e4e1ee`

## Partitions (chronological, one embargoed market between each)

- train: 22 markets
- validation: 6 markets
- untouched_test: 7 markets

## Results

| Strategy | Scenario | Partition | Traded mkts | Realized | Mean per market | 95% CI | Fees | Maker/Taker fills | Max DD | Unpaired at close | 5s markout | Stopped by |
|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|
| scanner_only | central | all | 0 | 0.0000 | n/a | n/a | 0.0000 | 0/0 | 0.0000 | n/a | n/a | - |
| taker_pair_research | central | all | 0 | 0.0000 | n/a | n/a | 0.0000 | 0/0 | 0.0000 | n/a | n/a | - |
| taker_pair_research | central_2x_latency | all | 0 | 0.0000 | n/a | n/a | 0.0000 | 0/0 | 0.0000 | n/a | n/a | - |
| passive_maker | pessimistic | all | 8 | -14.4292 | -1.8036 | -4.5217 to 0.9144 | 0.0000 | 104/0 | 17.2705 | 0.7500 | -0.0433 | loss_cap |
| passive_maker | central | all | 7 | -9.6050 | -1.3721 | -3.2760 to 0.5318 | 0.0325 | 95/1 | 10.6558 | 0.7143 | -0.0328 | loss_cap |
| passive_constrained_hedge | central | all | 7 | -9.6050 | -1.3721 | -3.2760 to 0.5318 | 0.0325 | 95/1 | 10.6558 | 0.7143 | -0.0328 | loss_cap |
| passive_maker_no_loss_stop | pessimistic | all | 36 | -108.7612 | -3.1989 | -4.3903 to -2.0075 | 0.3342 | 501/5 | 109.5612 | 0.8056 | -0.0341 | - |
| passive_maker_no_loss_stop | central | all | 36 | -91.8886 | -2.7026 | -3.8030 to -1.6023 | 0.4701 | 559/10 | 92.4386 | 0.9167 | -0.0276 | - |
| passive_maker_no_loss_stop | optimistic | all | 36 | -77.7656 | -2.2872 | -3.5048 to -1.0697 | 0.2485 | 573/6 | 78.2936 | 0.8611 | -0.0253 | - |
| passive_maker_no_loss_stop | pessimistic_2x_latency | all | 36 | -82.1131 | -2.4151 | -3.5353 to -1.2949 | 0.4276 | 528/7 | 84.4131 | 0.8333 | -0.0271 | - |
| passive_maker_no_loss_stop | central_2x_latency | all | 36 | -102.1340 | -3.0039 | -3.9218 to -2.0861 | 0.0795 | 567/2 | 103.7340 | 0.8611 | -0.0265 | - |
| passive_maker_no_loss_stop | central_higher_fees | all | 36 | -88.8954 | -2.6146 | -3.6973 to -1.5318 | 0.3423 | 529/5 | 89.7451 | 0.8889 | -0.0223 | - |
| passive_maker_no_loss_stop | central_late_hedge | all | 36 | -97.2503 | -2.8603 | -3.7163 to -2.0043 | 0.2958 | 557/7 | 97.8003 | 0.9167 | -0.0280 | - |
| passive_hedge_no_loss_stop | pessimistic | all | 36 | -105.3808 | -3.0994 | -4.0996 to -2.0993 | 1.1226 | 496/12 | 106.1808 | 0.8611 | -0.0327 | - |
| passive_hedge_no_loss_stop | central | all | 36 | -79.8839 | -2.3495 | -3.3478 to -1.3512 | 1.1794 | 547/14 | 80.4338 | 0.8889 | -0.0285 | - |
| passive_hedge_no_loss_stop | pessimistic_2x_latency | all | 36 | -79.0686 | -2.3255 | -3.4305 to -1.2206 | 0.9988 | 524/11 | 81.3686 | 0.8611 | -0.0273 | - |
| passive_maker_no_loss_stop | pessimistic | train | 22 | -69.8070 | -3.1730 | -4.7990 to -1.5471 | 0.1372 | 365/2 | 69.8070 | 0.8182 | -0.0281 | near_expiry |
| passive_maker_no_loss_stop | pessimistic | validation | 6 | -25.6095 | -4.2682 | -6.5863 to -1.9502 | 0.1419 | 63/2 | 27.7095 | 0.8333 | -0.0568 | near_expiry |
| passive_maker_no_loss_stop | pessimistic | untouched_test | 6 | -8.9447 | -2.2362 | -3.5355 to -0.9369 | 0.0551 | 47/1 | 9.7447 | 0.6667 | -0.0461 | - |
| passive_maker_no_loss_stop | central | train | 22 | -48.3808 | -2.1991 | -3.6811 to -0.7172 | 0.3098 | 415/7 | 56.3452 | 0.9545 | -0.0211 | near_expiry |
| passive_maker_no_loss_stop | central | validation | 6 | -23.4015 | -3.9002 | -6.1744 to -1.6261 | 0.0551 | 74/1 | 25.5015 | 0.8333 | -0.0449 | near_expiry |
| passive_maker_no_loss_stop | central | untouched_test | 6 | -9.1912 | -2.2978 | -3.6160 to -0.9796 | 0.0308 | 52/1 | 9.7411 | 0.6667 | -0.0437 | - |
| passive_hedge_no_loss_stop | pessimistic | train | 22 | -64.8708 | -2.9487 | -4.2647 to -1.6327 | 0.7785 | 356/8 | 64.8708 | 0.8636 | -0.0275 | near_expiry |
| passive_hedge_no_loss_stop | pessimistic | validation | 6 | -25.1421 | -4.1904 | -6.5921 to -1.7886 | 0.3414 | 62/3 | 27.2421 | 0.8333 | -0.0566 | near_expiry |
| passive_hedge_no_loss_stop | pessimistic | untouched_test | 6 | -10.7210 | -2.6802 | -4.7891 to -0.5714 | 0.2317 | 46/2 | 11.5210 | 0.6667 | -0.0459 | - |
| passive_hedge_no_loss_stop | central | train | 22 | -50.8583 | -2.3117 | -3.5980 to -1.0255 | 0.6978 | 392/9 | 50.8583 | 0.9545 | -0.0232 | near_expiry |
| passive_hedge_no_loss_stop | central | validation | 6 | -23.1421 | -3.8570 | -6.0071 to -1.7070 | 0.3414 | 71/3 | 25.2421 | 0.8333 | -0.0549 | near_expiry |
| passive_hedge_no_loss_stop | central | untouched_test | 6 | -9.6315 | -2.4079 | -4.5605 to -0.2553 | 0.2317 | 48/2 | 10.1815 | 0.6667 | -0.0434 | - |

## Assumptions

- Queue position is estimated, not observed: orders join behind visible size at their price.
- Unexplained size removal is bracketed by scenario; the feed cannot attribute cancels.
- Trade-through of a resting price is treated as a full fill of the order.
- Latency is a configured model with seeded jitter, not a measurement of this bot's real path.
- The bot's own simulated orders do not change other participants' behaviour.
- Maker rebates are excluded. Fees use the market's published schedule, rounded to 5 decimals.
- ADR-0002: BUY fees are modelled in shares received, the conservative treatment.
- Outcomes come only from official venue resolution fields recorded after close.
